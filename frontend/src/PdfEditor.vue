<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from "vue";
import * as pdfjs from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { applyPdfAnnotations } from "./pdf-export.js";
import { clamp, inverseRotatePointAroundCenter, normalizePoint, rotatedBounds, toCanvasPoint } from "./pdf-editor-geometry.js";
import { createImagePromiseCache, loadImageBatches, loadImageFromObjectUrl } from "./pdf-image-cache.js";
import { appendErasePoint, commitEraseDraft, discardEraseDraft, stageEraseStroke } from "./pdf-erase-draft.js";
import { releasePdfCanvas, resolvePdfCanvasPixelRatio } from "./pdf-render-sizing.js";
import { createEraseLayerCache } from "./pdf-erase-layer-cache.js";
import { drawSignatureImage, removeLightSignatureBackground } from "./pdf-signature-image.js";
import { appendSignaturePoint, drawSignatureStrokes, rebaseSignaturePointer } from "./pdf-signature-pad.js";
import { rebasePdfOverlayPointerAction } from "./pdf-overlay-pointer.js";
import { createFrameScheduler } from "./frame-scheduler.js";
import { focusElement } from "./focus-element.js";
import { safeDownloadStem } from "./download-filename.js";
import { downloadBlob, triggerBrowserDownload } from "./browser-download.js";
import DocumentWorkspace from "./DocumentWorkspace.vue";

const PDF_EXPORT_TARGET_DPI = 300;
const PDF_EXPORT_MAX_CANVAS_EDGE = 4096;
const PDF_EXPORT_MAX_CANVAS_PIXELS = 16_000_000;

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

const props = defineProps({
  file: { type: Object, required: true },
  active: { type: Boolean, default: true },
});
const emit = defineEmits(["toast", "save-copy", "close"]);

const pageCanvas = ref(null);
const overlayCanvas = ref(null);
const stage = ref(null);
const signatureInput = ref(null);
const drawCanvas = ref(null);
const drawDialogElement = ref(null);
const drawDialogCloseButton = ref(null);
const pendingEraseStrokes = ref([]);
const pdfDocument = shallowRef(null);
const pageIndex = ref(0);
const pageCount = ref(0);
const zoom = ref(1);
const pageSize = reactive({ width: 0, height: 0, pdfWidth: 0, pdfHeight: 0 });
const loading = ref(false);
const exporting = ref(false);
const errorMessage = ref("");
const mode = ref("select");
const brushWidth = ref(24);
const drawDialog = ref(false);
const drawBrush = ref(4);
const signatureOpacity = ref(1);
const editRevision = ref(0);
const exportFile = ref(null);
const exportHref = ref("");
const drawHasInk = ref(false);
const pageStates = reactive(new Map());
const imageCache = createImagePromiseCache(decodeImage);
const eraseLayerCache = createEraseLayerCache(() => document.createElement("canvas"));
let resizeObserver;
let drawResizeObserver;
let renderSequence = 0;
let renderTask = null;
let loadedFile = null;
let currentPageEraseColor = "#fff";
let previewTransitionSequence = 0;
let previewTransition = Promise.resolve();
let pointerAction = null;
let drawPointer = null;
let overlayRenderSequence = 0;
const drawStrokes = [];
let drawDialogReturnFocus = null;
const overlayScheduler = createFrameScheduler(() => { void drawOverlay(); });
const resizeRenderScheduler = createFrameScheduler(() => {
  if (props.active) void renderCurrentPage();
});

const currentState = computed(() => pageStates.get(pageIndex.value) || null);
const canUndo = computed(() => (currentState.value?.undo.length || 0) > 0);
const canRedo = computed(() => (currentState.value?.redo.length || 0) > 0);
const hasEdits = computed(() => {
  editRevision.value;
  return [...pageStates.values()].some((state) => state.strokes.length || state.signatures.length);
});
const hasFlattenedEdits = computed(() => {
  editRevision.value;
  return [...pageStates.values()].some((state) => state.strokes.length);
});
const selectedSignature = computed(() => {
  const current = currentState.value;
  return current?.signatures[current.selectedSignature] || null;
});
const outputName = computed(() => {
  const name = props.file?.name || "文档.pdf";
  const stem = safeDownloadStem(name.replace(/\.pdf$/i, "").replace(/(?:_编辑后)+$/i, ""), "文档");
  return `${stem}_编辑后.pdf`;
});

function readableSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function makePageState() {
  return { strokes: [], signatures: [], selectedSignature: -1, undo: [], redo: [] };
}

function stateForPage(index = pageIndex.value) {
  if (!pageStates.has(index)) pageStates.set(index, reactive(makePageState()));
  return pageStates.get(index);
}

function snapshot(state) {
  return {
    strokes: state.strokes.map((stroke) => ({ width: stroke.width, points: stroke.points.map((point) => ({ ...point })) })),
    signatures: state.signatures.map((signature) => ({ ...signature })),
    selectedSignature: state.selectedSignature,
  };
}

function restoreSnapshot(state, value) {
  state.strokes = value.strokes.map((stroke) => ({ width: stroke.width, points: stroke.points.map((point) => ({ ...point })) }));
  state.signatures = value.signatures.map((signature) => ({ ...signature }));
  state.selectedSignature = value.selectedSignature;
}

function pushUndo(state = stateForPage()) {
  state.undo.push(snapshot(state));
  if (state.undo.length > 40) state.undo.shift();
  state.redo = [];
}

function invalidateExport() {
  if (exportHref.value) URL.revokeObjectURL(exportHref.value);
  exportHref.value = "";
  exportFile.value = null;
}

function markChanged() {
  editRevision.value += 1;
  invalidateExport();
  drawOverlayNow();
}

function drawOverlayNow() {
  overlayScheduler.cancel();
  void drawOverlay();
}

function undo() {
  const state = stateForPage();
  if (!state.undo.length) return;
  state.redo.push(snapshot(state));
  restoreSnapshot(state, state.undo.pop());
  markChanged();
}

function redo() {
  const state = stateForPage();
  if (!state.redo.length) return;
  state.undo.push(snapshot(state));
  restoreSnapshot(state, state.redo.pop());
  markChanged();
}

async function loadFile(file) {
  const sequence = ++renderSequence;
  loadedFile = null;
  const previousRenderTask = renderTask;
  if (previousRenderTask) {
    try { previousRenderTask.cancel(); } catch { /* A completed render cannot be cancelled. */ }
    renderTask = null;
    try { await previousRenderTask.promise; } catch { /* Cancelled renders reject their promise. */ }
  }
  if (pdfDocument.value) {
    try { await pdfDocument.value.destroy(); } catch { /* Ignore cleanup errors from the previous PDF. */ }
  }
  pdfDocument.value = null;
  releasePdfCanvas(pageCanvas.value);
  releasePdfCanvas(overlayCanvas.value);
  eraseLayerCache.clear();
  pageStates.clear();
  discardEraseDraft(pendingEraseStrokes.value);
  imageCache.clear();
  pageIndex.value = 0;
  pageCount.value = 0;
  zoom.value = 1;
  Object.assign(pageSize, { width: 0, height: 0, pdfWidth: 0, pdfHeight: 0 });
  editRevision.value += 1;
  invalidateExport();
  errorMessage.value = "";
  if (!file) return;
  loading.value = true;
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const task = pdfjs.getDocument({ data: bytes });
    const document = await task.promise;
    if (sequence !== renderSequence) {
      await document.destroy();
      return;
    }
    pdfDocument.value = document;
    loadedFile = file;
    pageCount.value = document.numPages;
    await nextTick();
    await renderCurrentPage(sequence);
  } catch (error) {
    if (sequence === renderSequence) errorMessage.value = error?.message || "无法读取此 PDF";
  } finally {
    if (sequence === renderSequence) loading.value = false;
  }
}

async function renderCurrentPage(expectedSequence = renderSequence) {
  resizeRenderScheduler.cancel();
  const document = pdfDocument.value;
  const container = stage.value;
  const canvas = pageCanvas.value;
  const overlay = overlayCanvas.value;
  if (!props.active || !document || !container || !canvas || !overlay) return;
  if (renderTask) {
    try { renderTask.cancel(); } catch { /* Ignore a render that already finished. */ }
    renderTask = null;
  }
  const sequence = ++renderSequence;
  loading.value = true;
  errorMessage.value = "";
  try {
    const page = await document.getPage(pageIndex.value + 1);
    if (sequence !== renderSequence || expectedSequence > sequence) return;
    const baseViewport = page.getViewport({ scale: 1 });
    const availableWidth = Math.max(240, container.clientWidth - 42);
    const scale = Math.min(1.6, availableWidth / baseViewport.width) * zoom.value;
    const viewport = page.getViewport({ scale });
    const cssWidth = Math.ceil(viewport.width);
    const cssHeight = Math.ceil(viewport.height);
    const pixelRatio = resolvePdfCanvasPixelRatio(cssWidth, cssHeight, window.devicePixelRatio || 1);
    pageSize.width = cssWidth;
    pageSize.height = cssHeight;
    pageSize.pdfWidth = page.view[2] - page.view[0];
    pageSize.pdfHeight = page.view[3] - page.view[1];
    for (const target of [canvas, overlay]) {
      target.width = Math.ceil(cssWidth * pixelRatio);
      target.height = Math.ceil(cssHeight * pixelRatio);
      target.style.width = `${cssWidth}px`;
      target.style.height = `${cssHeight}px`;
    }
    const context = canvas.getContext("2d", { alpha: false });
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    renderTask = page.render({ canvasContext: context, viewport });
    await renderTask.promise;
    renderTask = null;
    if (sequence !== renderSequence) return;
    currentPageEraseColor = estimatePageEraseColor(canvas);
    await drawOverlay();
  } catch (error) {
    if (error?.name !== "RenderingCancelledException" && sequence === renderSequence) {
      errorMessage.value = error?.message || "PDF 页面渲染失败";
    }
  } finally {
    if (sequence === renderSequence) loading.value = false;
  }
}

function schedulePreviewSync() {
  resizeRenderScheduler.cancel();
  const sequence = ++previewTransitionSequence;
  if (!props.active) {
    renderSequence += 1;
    overlayRenderSequence += 1;
    overlayScheduler.cancel();
    const pendingRender = renderTask;
    if (pendingRender) {
      try { pendingRender.cancel(); } catch { /* Ignore a render that already finished. */ }
      renderTask = null;
    }
    previewTransition = previewTransition.catch(() => {}).then(async () => {
      if (pendingRender) {
        try { await pendingRender.promise; } catch { /* Cancelled renders reject their promise. */ }
      }
      if (sequence !== previewTransitionSequence || props.active) return;
      if (pdfDocument.value) {
        try { await pdfDocument.value.cleanup(); } catch { /* Keep the open document if cleanup is temporarily unavailable. */ }
      }
      if (sequence !== previewTransitionSequence || props.active) return;
      pointerAction = null;
      releasePdfCanvas(pageCanvas.value);
      releasePdfCanvas(overlayCanvas.value);
      eraseLayerCache.clear();
      Object.assign(pageSize, { width: 0, height: 0, pdfWidth: 0, pdfHeight: 0 });
      imageCache.clear();
      loading.value = false;
    });
    return previewTransition;
  }

  const file = props.file;
  previewTransition = previewTransition.catch(() => {}).then(async () => {
    if (sequence !== previewTransitionSequence || !props.active || !file) return;
    if (loadedFile !== file || !pdfDocument.value) {
      await loadFile(file);
      return;
    }
    await renderCurrentPage();
  });
  return previewTransition;
}

async function drawOverlay() {
  const canvas = overlayCanvas.value;
  const context = canvas?.getContext("2d");
  if (!canvas || !context || !pageSize.width || !pageSize.height) return;
  const drawSequence = ++overlayRenderSequence;
  const sequence = renderSequence;
  const pixelRatio = canvas.width / pageSize.width;
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, pageSize.width, pageSize.height);
  const state = stateForPage();
  const eraseLayer = eraseLayerCache.draw({
    pageIndex: pageIndex.value,
    strokes: state.strokes,
    width: canvas.width,
    height: canvas.height,
    pageWidth: pageSize.width,
    pageHeight: pageSize.height,
    pixelRatio,
    paint: (layerContext, strokes) => paintEraseStrokes(layerContext, strokes, currentPageEraseColor),
  });
  if (eraseLayer) {
    context.save();
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.drawImage(eraseLayer, 0, 0);
    context.restore();
  }
  context.save();
  context.lineCap = "round";
  context.lineJoin = "round";
  if (!eraseLayer) paintEraseStrokes(context, state.strokes, currentPageEraseColor);
  paintEraseStrokes(context, pendingEraseStrokes.value, "rgba(95, 91, 231, .38)");
  context.restore();

  const signatures = state.signatures.slice();
  for await (const { start, images } of loadImageBatches(signatures, signature => loadImage(signature.dataUrl))) {
    if (drawSequence !== overlayRenderSequence || sequence !== renderSequence || canvas !== overlayCanvas.value) return;
    for (let offset = 0; offset < images.length; offset += 1) {
      const index = start + offset;
      const signature = signatures[index];
      const image = images[offset];
      if (!image) continue;
      const width = signature.width * pageSize.width;
      const height = signature.height * pageSize.height;
      drawSignatureImage(context, image, signature, pageSize.width, pageSize.height);
      if (index === state.selectedSignature && mode.value === "signature") {
        context.save();
        context.translate((signature.x + signature.width / 2) * pageSize.width, (signature.y + signature.height / 2) * pageSize.height);
        context.rotate((signature.rotation || 0) * Math.PI / 180);
        context.strokeStyle = "#5f5be7";
        context.lineWidth = 1.5;
        context.setLineDash([5, 4]);
        context.strokeRect(-width / 2, -height / 2, width, height);
        context.setLineDash([]);
        context.fillStyle = "#5f5be7";
        context.fillRect(width / 2 - 6, height / 2 - 6, 13, 13);
        context.restore();
      }
    }
  }
}

function paintEraseStrokes(context, strokes, color) {
  context.strokeStyle = color;
  context.fillStyle = color;
  for (const stroke of strokes) {
    context.lineWidth = stroke.width * pageSize.width;
    if (stroke.points.length === 1) {
      const point = toCanvasPoint(stroke.points[0], pageSize.width, pageSize.height);
      context.beginPath();
      context.arc(point.x, point.y, context.lineWidth / 2, 0, Math.PI * 2);
      context.fill();
      continue;
    }
    context.beginPath();
    stroke.points.forEach((point, index) => {
      const mapped = toCanvasPoint(point, pageSize.width, pageSize.height);
      if (index === 0) context.moveTo(mapped.x, mapped.y);
      else context.lineTo(mapped.x, mapped.y);
    });
    context.stroke();
  }
}

function estimatePageEraseColor(sourceCanvas) {
  const sampleCanvas = document.createElement("canvas");
  sampleCanvas.width = 40;
  sampleCanvas.height = 40;
  const context = sampleCanvas.getContext("2d", { willReadFrequently: true });
  if (!context) {
    releasePdfCanvas(sampleCanvas);
    return "#fff";
  }

  try {
    context.drawImage(sourceCanvas, 0, 0, sampleCanvas.width, sampleCanvas.height);
    const { data } = context.getImageData(0, 0, sampleCanvas.width, sampleCanvas.height);
    const colors = [];
    for (let index = 0; index < data.length; index += 4) {
      const red = data[index];
      const green = data[index + 1];
      const blue = data[index + 2];
      colors.push({
        red,
        green,
        blue,
        luminance: red * 0.2126 + green * 0.7152 + blue * 0.0722,
      });
    }
    colors.sort((first, second) => first.luminance - second.luminance);
    const paperSamples = colors.slice(Math.floor(colors.length * 0.7));
    const medianChannel = channel => {
      const values = paperSamples.map(color => color[channel]).sort((first, second) => first - second);
      return values[Math.floor(values.length / 2)];
    };
    const paper = [medianChannel("red"), medianChannel("green"), medianChannel("blue")];
    const luminance = paper[0] * 0.2126 + paper[1] * 0.7152 + paper[2] * 0.0722;
    if (luminance < 172 || Math.max(...paper) - Math.min(...paper) > 52) return "#fff";
    return `rgb(${paper.join(", ")})`;
  } catch {
    return "#fff";
  } finally {
    releasePdfCanvas(sampleCanvas);
  }
}

function decodeImage(source) {
  return new Promise((resolve) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => resolve(null);
    image.src = source;
  });
}

function loadImage(dataUrl) {
  return imageCache.load(dataUrl);
}

function normalizedPoint(event, rect = overlayCanvas.value.getBoundingClientRect()) {
  return { ...normalizePoint(event.clientX, event.clientY, rect), rect };
}

function signatureAt(state, x, y) {
  for (let index = state.signatures.length - 1; index >= 0; index -= 1) {
    const item = state.signatures[index];
    const local = signatureLocalPoint(item, { x, y });
    if (local.x >= 0 && local.x <= item.width * pageSize.width
      && local.y >= 0 && local.y <= item.height * pageSize.height) return index;
  }
  return -1;
}

function signatureLocalPoint(signature, point) {
  const centerX = (signature.x + signature.width / 2) * pageSize.width;
  const centerY = (signature.y + signature.height / 2) * pageSize.height;
  const local = inverseRotatePointAroundCenter(
    point.x * pageSize.width,
    point.y * pageSize.height,
    centerX,
    centerY,
    signature.rotation || 0,
  );
  return {
    x: local.x - centerX + signature.width * pageSize.width / 2,
    y: local.y - centerY + signature.height * pageSize.height / 2,
  };
}

function clampSignatureToPage(item) {
  let width = item.width * pageSize.width;
  let height = item.height * pageSize.height;
  const bounds = rotatedBounds(width, height, item.rotation || 0);
  const fit = Math.min(1, pageSize.width / bounds.width, pageSize.height / bounds.height);
  if (fit < 1) {
    item.width *= fit;
    item.height *= fit;
    width *= fit;
    height *= fit;
  }
  const fittedBounds = rotatedBounds(width, height, item.rotation || 0);
  const centerX = clamp((item.x + item.width / 2) * pageSize.width, fittedBounds.width / 2, pageSize.width - fittedBounds.width / 2);
  const centerY = clamp((item.y + item.height / 2) * pageSize.height, fittedBounds.height / 2, pageSize.height - fittedBounds.height / 2);
  item.x = centerX / pageSize.width - item.width / 2;
  item.y = centerY / pageSize.height - item.height / 2;
}

function onOverlayPointerDown(event) {
  if (!overlayCanvas.value || loading.value || exporting.value) return;
  overlayCanvas.value.setPointerCapture(event.pointerId);
  const point = normalizedPoint(event);
  const state = stateForPage();
  if (mode.value === "erase") {
    const stroke = { width: brushWidth.value / point.rect.width, points: [{ x: point.x, y: point.y }] };
    stageEraseStroke(pendingEraseStrokes.value, stroke);
    pointerAction = { type: "erase", stroke, rect: point.rect, clientX: event.clientX, clientY: event.clientY };
    drawOverlayNow();
    return;
  }
  if (mode.value !== "signature") return;
  const selected = state.signatures[state.selectedSignature];
  if (selected) {
    const local = signatureLocalPoint(selected, point);
    const nearHandle = Math.hypot(local.x - selected.width * pageSize.width, local.y - selected.height * pageSize.height) <= 18;
    if (nearHandle) {
      pushUndo(state);
      pointerAction = {
        type: "resize",
        item: selected,
        startX: point.x * pageSize.width,
        startY: point.y * pageSize.height,
        startWidth: selected.width,
        startHeight: selected.height,
        rotation: selected.rotation || 0,
        rect: point.rect,
        clientX: event.clientX,
        clientY: event.clientY,
      };
      return;
    }
  }
  const hit = signatureAt(state, point.x, point.y);
  if (hit >= 0) {
    state.selectedSignature = hit;
    const item = state.signatures[hit];
    pushUndo(state);
    pointerAction = {
      type: "move",
      item,
      startX: point.x,
      startY: point.y,
      x: item.x,
      y: item.y,
      rect: point.rect,
      clientX: event.clientX,
      clientY: event.clientY,
    };
    signatureOpacity.value = item.opacity;
  } else {
    state.selectedSignature = -1;
    pointerAction = null;
  }
  editRevision.value += 1;
  drawOverlayNow();
}

function onOverlayPointerMove(event) {
  if (!pointerAction || exporting.value) return;
  pointerAction.clientX = event.clientX;
  pointerAction.clientY = event.clientY;
  const point = normalizedPoint(event, pointerAction.rect);
  let changed = true;
  if (pointerAction.type === "erase") {
    const sampleDistance = Math.min(2, pointerAction.stroke.width * point.rect.width * 0.2);
    changed = appendErasePoint(pointerAction.stroke, point, point.rect, sampleDistance);
  } else if (pointerAction.type === "move") {
    const item = pointerAction.item;
    item.x = pointerAction.x + point.x - pointerAction.startX;
    item.y = pointerAction.y + point.y - pointerAction.startY;
    clampSignatureToPage(item);
  } else if (pointerAction.type === "resize") {
    const item = pointerAction.item;
    const imageAspect = item.pixelWidth / item.pixelHeight;
    const pageAspect = point.rect.width / point.rect.height;
    const deltaX = point.x * pageSize.width - pointerAction.startX;
    const deltaY = point.y * pageSize.height - pointerAction.startY;
    const radians = pointerAction.rotation * Math.PI / 180;
    const localDeltaX = deltaX * Math.cos(radians) + deltaY * Math.sin(radians);
    const localDeltaY = -deltaX * Math.sin(radians) + deltaY * Math.cos(radians);
    const diagonalX = pointerAction.startWidth * pageSize.width / 2;
    const diagonalY = pointerAction.startHeight * pageSize.height / 2;
    const diagonalLength = diagonalX ** 2 + diagonalY ** 2;
    const scale = Math.max(0.05, 1 + (2 * (localDeltaX * diagonalX + localDeltaY * diagonalY)) / diagonalLength);
    const maxWidth = Math.min(0.9, (pageSize.height / pageSize.width) * imageAspect);
    const width = clamp(pointerAction.startWidth * scale, 0.04, maxWidth);
    const height = width * pageAspect / imageAspect;
    const centerX = item.x + item.width / 2;
    const centerY = item.y + item.height / 2;
    item.width = width;
    item.height = height;
    item.x = centerX - width / 2;
    item.y = centerY - height / 2;
    clampSignatureToPage(item);
  }
  if (!changed) return;
  invalidateExport();
  overlayScheduler.schedule();
}

function onOverlayPointerUp(event) {
  if (!pointerAction) return;
  const actionType = pointerAction.type;
  if (actionType === "erase" && event && overlayCanvas.value) {
    const point = normalizedPoint(event, pointerAction.rect);
    appendErasePoint(pointerAction.stroke, point, point.rect, 0);
  }
  pointerAction = null;
  if (actionType === "erase") {
    drawOverlayNow();
    return;
  }
  markChanged();
}

function confirmErase() {
  const draft = pendingEraseStrokes.value;
  if (!draft.length) return;
  const state = stateForPage();
  pushUndo(state);
  commitEraseDraft(draft, state);
  markChanged();
  emit("toast", "已确认擦除区域，可撤销恢复");
}

function cancelErasePreview() {
  if (!pendingEraseStrokes.value.length) return;
  discardEraseDraft(pendingEraseStrokes.value);
  drawOverlayNow();
}

function setMode(value) {
  mode.value = value;
  drawOverlayNow();
}

function changePage(value) {
  const next = clamp(pageIndex.value + value, 0, pageCount.value - 1);
  if (next === pageIndex.value) return;
  if (pendingEraseStrokes.value.length) {
    discardEraseDraft(pendingEraseStrokes.value);
    emit("toast", "未确认的擦除预览已清除");
  }
  pageIndex.value = next;
  renderCurrentPage();
}

function changeZoom(value) {
  zoom.value = clamp(Number((zoom.value + value).toFixed(2)), 0.5, 2.5);
  renderCurrentPage();
}

async function normalizeImageFile(file) {
  if (!file || !file.type.startsWith("image/")) throw new Error("请选择 PNG、JPG 等签名图片");
  const source = await loadImageFromObjectUrl(file, decodeImage);
  if (!source) throw new Error("无法读取签名图片");
  const ratio = Math.min(1, 2400 / Math.max(source.naturalWidth, source.naturalHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(source.naturalWidth * ratio));
  canvas.height = Math.max(1, Math.round(source.naturalHeight * ratio));
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
  removeLightSignatureBackground(pixels);
  context.putImageData(pixels, 0, 0);
  return { dataUrl: canvas.toDataURL("image/png"), width: canvas.width, height: canvas.height };
}

async function addSignature(dataUrl, width, height) {
  if (!pageSize.width || !pageSize.height) return;
  const state = stateForPage();
  pushUndo(state);
  const imageAspect = width / height;
  const pageAspect = pageSize.width / pageSize.height;
  let signatureWidth = 0.3;
  let signatureHeight = signatureWidth * pageAspect / imageAspect;
  if (signatureHeight > 0.8) {
    signatureHeight = 0.8;
    signatureWidth = signatureHeight * imageAspect / pageAspect;
  }
  state.signatures.push({
    dataUrl,
    pixelWidth: width,
    pixelHeight: height,
    x: clamp(0.5 - signatureWidth / 2, 0, 1 - signatureWidth),
    y: clamp(0.52 - signatureHeight / 2, 0, 1 - signatureHeight),
    width: signatureWidth,
    height: Math.min(signatureHeight, 0.8),
    opacity: 1,
    rotation: 0,
  });
  state.selectedSignature = state.signatures.length - 1;
  signatureOpacity.value = 1;
  mode.value = "signature";
  markChanged();
  emit("toast", "签名图片浅色背景已转透明，可拖动、旋转、缩放或删除");
}

async function onSignatureFileChange(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  try {
    const image = await normalizeImageFile(file);
    await addSignature(image.dataUrl, image.width, image.height);
  } catch (error) {
    emit("toast", error?.message || "签名图片导入失败");
  }
}

function removeSelectedSignature() {
  const state = stateForPage();
  if (state.selectedSignature < 0) return;
  pushUndo(state);
  state.signatures.splice(state.selectedSignature, 1);
  state.selectedSignature = -1;
  markChanged();
}

function beginOpacityEdit() {
  const state = stateForPage();
  if (state.selectedSignature >= 0) pushUndo(state);
}

function beginSignatureTransform() {
  const state = stateForPage();
  if (state.selectedSignature >= 0) pushUndo(state);
}

function updateSignatureOpacity(value) {
  const signature = selectedSignature.value;
  if (!signature) return;
  signature.opacity = Number(value);
  signatureOpacity.value = signature.opacity;
  markChanged();
}

function updateSignatureRotation(value) {
  const signature = selectedSignature.value;
  if (!signature) return;
  signature.rotation = Number(value);
  clampSignatureToPage(signature);
  markChanged();
}

function updateSignatureScale(value) {
  const signature = selectedSignature.value;
  if (!signature) return;
  const pageAspect = pageSize.width / pageSize.height;
  const imageAspect = signature.pixelWidth / signature.pixelHeight;
  const maxWidth = Math.min(0.9, imageAspect / pageAspect);
  const width = clamp(Number(value) / 100, 0.04, maxWidth);
  const height = width * pageAspect / imageAspect;
  const centerX = signature.x + signature.width / 2;
  const centerY = signature.y + signature.height / 2;
  signature.width = width;
  signature.height = height;
  signature.x = centerX - width / 2;
  signature.y = centerY - height / 2;
  clampSignatureToPage(signature);
  markChanged();
}

function rotateSelectedSignature(delta) {
  const signature = selectedSignature.value;
  if (!signature) return;
  pushUndo(stateForPage());
  signature.rotation = ((Number(signature.rotation || 0) + delta + 540) % 360) - 180;
  clampSignatureToPage(signature);
  markChanged();
}

function resizeDrawCanvas() {
  const canvas = drawCanvas.value;
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  if (drawPointer) rebaseSignaturePointer(drawPointer, rect);
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  drawSignatureStrokes(context, drawStrokes, rect.width, rect.height);
  drawHasInk.value = drawStrokes.length > 0;
}

async function openDrawDialog(event) {
  drawDialogReturnFocus = event?.currentTarget || document.activeElement;
  drawStrokes.length = 0;
  drawPointer = null;
  drawHasInk.value = false;
  drawDialog.value = true;
  await nextTick();
  if (!drawDialogElement.value?.open) drawDialogElement.value?.showModal();
  focusElement(drawDialogCloseButton.value);
  resizeDrawCanvas();
  drawResizeObserver?.disconnect();
  drawResizeObserver = new ResizeObserver(resizeDrawCanvas);
  if (drawCanvas.value) drawResizeObserver.observe(drawCanvas.value);
}

function closeDrawDialog() {
  const returnFocus = drawDialogReturnFocus;
  drawResizeObserver?.disconnect();
  drawResizeObserver = null;
  drawPointer = null;
  drawStrokes.length = 0;
  drawHasInk.value = false;
  if (drawDialogElement.value?.open) drawDialogElement.value.close();
  drawDialog.value = false;
  drawDialogReturnFocus = null;
  void nextTick(() => {
    focusElement(returnFocus);
  });
}

function onDrawDialogClick(event) {
  const dialog = drawDialogElement.value;
  if (!dialog || event.target !== dialog) return;
  const bounds = dialog.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) {
    closeDrawDialog();
  }
}

function drawPointerDown(event) {
  const canvas = drawCanvas.value;
  if (!canvas) return;
  canvas.setPointerCapture(event.pointerId);
  const rect = canvas.getBoundingClientRect();
  const point = normalizePoint(event.clientX, event.clientY, rect);
  const stroke = { width: Number(drawBrush.value), points: [point] };
  drawStrokes.push(stroke);
  drawPointer = { point, stroke, rect, clientX: event.clientX, clientY: event.clientY };
  drawHasInk.value = true;
  drawSignatureStrokes(canvas.getContext("2d"), drawStrokes, rect.width, rect.height);
}

function drawPointerMove(event) {
  if (!drawPointer || !drawCanvas.value) return;
  const canvas = drawCanvas.value;
  const rect = drawPointer.rect;
  const point = normalizePoint(event.clientX, event.clientY, rect);
  const context = canvas.getContext("2d");
  context.strokeStyle = "#172033";
  context.lineWidth = drawPointer.stroke.width;
  context.beginPath();
  context.moveTo(drawPointer.point.x * rect.width, drawPointer.point.y * rect.height);
  context.lineTo(point.x * rect.width, point.y * rect.height);
  context.stroke();
  const sampleDistance = Math.min(1.5, drawPointer.stroke.width * 0.25);
  appendSignaturePoint(drawPointer.stroke, point, rect.width, rect.height, sampleDistance);
  drawPointer.point = point;
  drawPointer.clientX = event.clientX;
  drawPointer.clientY = event.clientY;
}

function endDrawPointer(event) {
  if (drawPointer && drawCanvas.value && event) {
    const rect = drawPointer.rect;
    const point = normalizePoint(event.clientX, event.clientY, rect);
    const context = drawCanvas.value.getContext("2d");
    context.strokeStyle = "#172033";
    context.lineWidth = drawPointer.stroke.width;
    context.beginPath();
    context.moveTo(drawPointer.point.x * rect.width, drawPointer.point.y * rect.height);
    context.lineTo(point.x * rect.width, point.y * rect.height);
    context.stroke();
    appendSignaturePoint(drawPointer.stroke, point, rect.width, rect.height, 0);
    drawPointer.point = point;
  }
  drawPointer = null;
}

function clearDrawCanvas() {
  drawPointer = null;
  drawStrokes.length = 0;
  drawHasInk.value = false;
  const canvas = drawCanvas.value;
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  drawSignatureStrokes(canvas.getContext("2d"), drawStrokes, rect.width, rect.height);
}

function cropDrawnSignature() {
  const source = drawCanvas.value;
  const context = source?.getContext("2d");
  if (!source || !context) return null;
  const image = context.getImageData(0, 0, source.width, source.height);
  let left = source.width;
  let top = source.height;
  let right = 0;
  let bottom = 0;
  for (let y = 0; y < source.height; y += 2) {
    for (let x = 0; x < source.width; x += 2) {
      if (image.data[(y * source.width + x) * 4 + 3] > 10) {
        left = Math.min(left, x); top = Math.min(top, y);
        right = Math.max(right, x); bottom = Math.max(bottom, y);
      }
    }
  }
  if (right <= left || bottom <= top) return null;
  const padding = 16;
  left = Math.max(0, left - padding); top = Math.max(0, top - padding);
  right = Math.min(source.width, right + padding); bottom = Math.min(source.height, bottom + padding);
  const cropped = document.createElement("canvas");
  cropped.width = right - left; cropped.height = bottom - top;
  cropped.getContext("2d").drawImage(source, left, top, cropped.width, cropped.height, 0, 0, cropped.width, cropped.height);
  return { dataUrl: cropped.toDataURL("image/png"), width: cropped.width, height: cropped.height };
}

async function useDrawnSignature() {
  const image = cropDrawnSignature();
  if (!image) {
    emit("toast", "请先在签名框里写下签名");
    return;
  }
  await addSignature(image.dataUrl, image.width, image.height);
  closeDrawDialog();
}

async function createEditedFile(states = pageStates) {
  if (!props.file) throw new Error("请先导入 PDF");
  const bytes = await applyPdfAnnotations(
    new Uint8Array(await props.file.arrayBuffer()),
    states,
    renderEditedPage,
    renderSignatureOverlay,
  );
  return new File([bytes], outputName.value, { type: "application/pdf", lastModified: Date.now() });
}

function resolveEditedPageScale(width, height) {
  const maxScale = Math.min(
    PDF_EXPORT_TARGET_DPI / 72,
    PDF_EXPORT_MAX_CANVAS_EDGE / Math.max(width, height),
    Math.sqrt(PDF_EXPORT_MAX_CANVAS_PIXELS / (width * height)),
  );
  const fits = scale => {
    const bitmapWidth = Math.ceil(width * scale);
    const bitmapHeight = Math.ceil(height * scale);
    return bitmapWidth <= PDF_EXPORT_MAX_CANVAS_EDGE
      && bitmapHeight <= PDF_EXPORT_MAX_CANVAS_EDGE
      && bitmapWidth * bitmapHeight <= PDF_EXPORT_MAX_CANVAS_PIXELS;
  };
  if (fits(maxScale)) return maxScale;

  let lower = 0;
  let upper = maxScale;
  for (let index = 0; index < 40; index += 1) {
    const middle = (lower + upper) / 2;
    if (fits(middle)) lower = middle;
    else upper = middle;
  }
  return lower;
}

async function renderEditedPage(index, state) {
  if (!pdfDocument.value) throw new Error("原始 PDF 已关闭，请重新导入后再试");
  const page = await pdfDocument.value.getPage(index + 1);
  const baseViewport = page.getViewport({ scale: 1 });
  // Render at print resolution when the page fits, while keeping the existing
  // canvas edge and area limits for unusually large pages and mobile browsers.
  const scale = resolveEditedPageScale(baseViewport.width, baseViewport.height);
  const viewport = page.getViewport({ scale });
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.ceil(viewport.width));
  canvas.height = Math.max(1, Math.ceil(viewport.height));
  const context = canvas.getContext("2d", { alpha: false });
  let outputCanvas = canvas;
  let canvasReleased = false;
  let blob;
  try {
    if (!context) throw new Error("无法创建 PDF 导出画布");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    await page.render({
      canvasContext: context,
      viewport,
      annotationMode: pdfjs.AnnotationMode.DISABLE,
    }).promise;

    const eraseColor = state.strokes.length ? estimatePageEraseColor(canvas) : "#fff";
    context.save();
    context.lineCap = "round";
    context.lineJoin = "round";
    context.strokeStyle = eraseColor;
    context.fillStyle = eraseColor;
    for (const stroke of state.strokes) {
      context.lineWidth = Math.max(1, stroke.width * canvas.width);
      if (stroke.points.length === 1) {
        const point = toCanvasPoint(stroke.points[0], canvas.width, canvas.height);
        context.beginPath();
        context.arc(point.x, point.y, context.lineWidth / 2, 0, Math.PI * 2);
        context.fill();
        continue;
      }
      context.beginPath();
      stroke.points.forEach((point, pointIndex) => {
        const mapped = toCanvasPoint(point, canvas.width, canvas.height);
        if (pointIndex === 0) context.moveTo(mapped.x, mapped.y);
        else context.lineTo(mapped.x, mapped.y);
      });
      context.stroke();
    }
    context.restore();

    await drawPageSignatures(context, canvas, state);

    const rotation = ((page.rotate % 360) + 360) % 360;
    if (rotation) {
      outputCanvas = rotateCanvasToPageSpace(canvas, page, scale, rotation);
      releasePdfCanvas(canvas);
      canvasReleased = true;
    }
    const pageImageType = state.strokes.length ? "image/png" : "image/jpeg";
    const pageImageQuality = pageImageType === "image/jpeg" ? 0.98 : undefined;
    blob = await new Promise((resolve, reject) => {
      outputCanvas.toBlob(
        result => result ? resolve(result) : reject(new Error("PDF 页面图像生成失败")),
        pageImageType,
        pageImageQuality,
      );
    });
  } finally {
    // Release both bitmaps before allocating the encoded data as a Uint8Array.
    if (!canvasReleased) releasePdfCanvas(canvas);
    if (outputCanvas !== canvas) releasePdfCanvas(outputCanvas);
  }
  return new Uint8Array(await blob.arrayBuffer());
}

async function renderSignatureOverlay(index, state) {
  if (!pdfDocument.value) throw new Error("原始 PDF 已关闭，请重新导入后再试");
  const page = await pdfDocument.value.getPage(index + 1);
  const baseViewport = page.getViewport({ scale: 1 });
  const scale = resolveEditedPageScale(baseViewport.width, baseViewport.height);
  const viewport = page.getViewport({ scale });
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.ceil(viewport.width));
  canvas.height = Math.max(1, Math.ceil(viewport.height));
  const context = canvas.getContext("2d", { alpha: true });
  let outputCanvas = canvas;
  let canvasReleased = false;
  let blob;
  try {
    if (!context) throw new Error("无法创建 PDF 导出画布");
    await drawPageSignatures(context, canvas, state);

    const rotation = ((page.rotate % 360) + 360) % 360;
    if (rotation) {
      outputCanvas = rotateCanvasToPageSpace(
        canvas,
        page,
        scale,
        rotation,
        { transparent: true },
      );
      releasePdfCanvas(canvas);
      canvasReleased = true;
    }
    blob = await new Promise((resolve, reject) => {
      outputCanvas.toBlob(
        result => result ? resolve(result) : reject(new Error("签名图像生成失败")),
        "image/png",
      );
    });
  } finally {
    if (!canvasReleased) releasePdfCanvas(canvas);
    if (outputCanvas !== canvas) releasePdfCanvas(outputCanvas);
  }
  return new Uint8Array(await blob.arrayBuffer());
}

async function drawPageSignatures(context, canvas, state) {
  const signatures = state.signatures.map(signature => ({ ...signature }));
  for await (const { start, images } of loadImageBatches(
    signatures,
    signature => loadImage(signature.dataUrl),
  )) {
    for (let offset = 0; offset < images.length; offset += 1) {
      const image = images[offset];
      if (!image) throw new Error("无法渲染签名图片");
      drawSignatureImage(
        context,
        image,
        signatures[start + offset],
        canvas.width,
        canvas.height,
      );
    }
  }
}

function rotateCanvasToPageSpace(canvas, page, scale, rotation, { transparent = false } = {}) {
  const viewport = page.getViewport({ scale, rotation: 0 });
  const uprightCanvas = document.createElement("canvas");
  uprightCanvas.width = Math.max(1, Math.ceil(viewport.width));
  uprightCanvas.height = Math.max(1, Math.ceil(viewport.height));
  const context = uprightCanvas.getContext("2d", { alpha: transparent });
  if (!context) {
    releasePdfCanvas(uprightCanvas);
    throw new Error("无法创建 PDF 导出画布");
  }
  if (!transparent) {
    context.fillStyle = "#fff";
    context.fillRect(0, 0, uprightCanvas.width, uprightCanvas.height);
  }
  context.translate(uprightCanvas.width / 2, uprightCanvas.height / 2);
  context.rotate(-rotation * Math.PI / 180);
  context.drawImage(canvas, -canvas.width / 2, -canvas.height / 2);
  return uprightCanvas;
}

async function prepareExport() {
  if (exporting.value) return null;
  exporting.value = true;
  errorMessage.value = "";
  const revision = editRevision.value;
  try {
    const states = new Map(
      [...pageStates].map(([index, state]) => [index, snapshot(state)]),
    );
    const file = await createEditedFile(states);
    if (revision !== editRevision.value) {
      throw new Error("导出期间编辑内容发生变化，请重新导出");
    }
    invalidateExport();
    exportFile.value = file;
    exportHref.value = URL.createObjectURL(file);
    return file;
  } catch (error) {
    errorMessage.value = error?.message || "无法生成编辑后的 PDF";
    return null;
  } finally {
    exporting.value = false;
  }
}

async function downloadEdited() {
  if (!hasEdits.value) {
    emit("toast", "请先擦除页面内容或添加签名");
    return;
  }
  const file = exportFile.value || await prepareExport();
  if (!file) return;
  triggerBrowserDownload(exportHref.value, outputName.value);
  emit("toast", "编辑版 PDF 已开始下载");
}

async function downloadPdf() {
  if (!props.file || loading.value || exporting.value) return;
  if (hasEdits.value) {
    await downloadEdited();
    return;
  }

  downloadBlob(props.file, props.file.name || "文档.pdf");
  emit("toast", "PDF 已开始下载");
}

async function saveEditedCopy() {
  if (!hasEdits.value || loading.value || exporting.value) return;
  const file = exportFile.value || await prepareExport();
  if (file) emit("save-copy", file);
}

async function sharePdf() {
  const file = hasEdits.value ? (exportFile.value || await prepareExport()) : props.file;
  if (!file) return;
  try {
    if (navigator.share && navigator.canShare?.({ files: [file] })) {
      await navigator.share({ files: [file], title: file.name, text: "来自 DocuCraft 的 PDF 文档" });
    } else if (hasEdits.value) {
      await downloadEdited();
      emit("toast", "浏览器不支持直接分享，已下载编辑版 PDF");
    } else {
      downloadBlob(file, file.name || "文档.pdf");
      emit("toast", "浏览器不支持直接分享，已下载 PDF，可发送该文件");
    }
  } catch (error) {
    if (error?.name !== "AbortError") emit("toast", "分享未完成，可下载 PDF 后发送");
  }
}

function restoreOriginal() {
  if (!hasEdits.value || !window.confirm("恢复后会清除当前所有擦除区域和签名，是否继续？")) return;
  discardEraseDraft(pendingEraseStrokes.value);
  pageStates.clear();
  editRevision.value += 1;
  invalidateExport();
  drawOverlayNow();
  emit("toast", "已恢复原始 PDF 页面");
}

function closeEditor() {
  if (hasEdits.value && !window.confirm("PDF 修改尚未保存，关闭后会丢弃这些修改。确定关闭吗？")) return;
  emit("close");
}

defineExpose({ sharePdf, downloadPdf, hasEdits });

watch(() => [props.file, props.active], schedulePreviewSync, { flush: "post" });

onMounted(() => {
  resizeObserver = new ResizeObserver(() => {
    if (pointerAction && overlayCanvas.value) {
      rebasePdfOverlayPointerAction(pointerAction, overlayCanvas.value.getBoundingClientRect(), pageSize);
    }
    if (props.active) resizeRenderScheduler.schedule();
  });
  if (stage.value) resizeObserver.observe(stage.value);
  if (overlayCanvas.value) resizeObserver.observe(overlayCanvas.value);
  schedulePreviewSync();
});

onBeforeUnmount(() => {
  previewTransitionSequence += 1;
  renderSequence += 1;
  overlayRenderSequence += 1;
  overlayScheduler.cancel();
  resizeRenderScheduler.cancel();
  eraseLayerCache.clear();
  resizeObserver?.disconnect();
  drawResizeObserver?.disconnect();
  if (renderTask) {
    try { renderTask.cancel(); } catch { /* Ignore cleanup errors. */ }
  }
  if (pdfDocument.value) pdfDocument.value.destroy();
  invalidateExport();
  imageCache.clear();
});
</script>

<template>
  <DocumentWorkspace class="pdf-editor" :title="file.name" :details="readableSize(file.size)">
    <template #actions>
      <div class="pdf-editor-actions">
        <button class="ghost-button" :disabled="!canUndo || loading" @click="undo">撤销</button>
        <button class="ghost-button" :disabled="!canRedo || loading" @click="redo">重做</button>
        <button class="primary-button small-button" :disabled="!hasEdits || loading || exporting" title="将修改保存为新的本地 PDF 副本，原文件保留" @click="saveEditedCopy">保存副本</button>
        <button class="ghost-button" :disabled="loading || exporting" @click="downloadPdf">下载 PDF</button>
        <button class="ghost-button" :disabled="loading || exporting" @click="sharePdf">分享</button>
        <button class="icon-button document-workspace-close" type="button" aria-label="关闭 PDF 编辑器" title="关闭" :disabled="loading || exporting" @click="closeEditor">
          <svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 6 12 12M18 6 6 18" /></svg>
        </button>
      </div>
    </template>

    <template #toolbar>
      <div class="pdf-editor-toolbar" :inert="exporting">
        <div class="pdf-tool-group" role="group" aria-label="编辑工具">
          <button type="button" class="pdf-tool-button" :class="{ 'is-active': mode === 'erase' }" :aria-pressed="mode === 'erase'" @click="setMode(mode === 'erase' ? 'select' : 'erase')">擦除文字</button>
          <button type="button" class="pdf-tool-button" :class="{ 'is-active': mode === 'signature' }" :aria-pressed="mode === 'signature'" @click="setMode(mode === 'signature' ? 'select' : 'signature')">移动签名</button>
          <button type="button" class="pdf-tool-button" title="自动将浅色背景转透明，导入后可旋转、缩放和删除" @click="signatureInput?.click()">添加图片签名</button>
          <button type="button" class="pdf-tool-button" @click="openDrawDialog">手写签名</button>
          <input ref="signatureInput" class="visually-hidden" type="file" accept="image/png,image/jpeg,image/webp" @change="onSignatureFileChange" />
        </div>
        <div v-if="mode === 'erase'" class="pdf-tool-options">
          <label>笔刷 <input v-model.number="brushWidth" type="range" min="10" max="72" step="2" /> {{ brushWidth }}px</label>
          <span>涂抹后确认，才会擦除</span>
          <button type="button" class="pdf-tool-button pdf-confirm-erase" :disabled="!pendingEraseStrokes.length" @click="confirmErase">确认擦除</button>
          <button v-if="pendingEraseStrokes.length" type="button" class="pdf-tool-button" @click="cancelErasePreview">取消预览</button>
        </div>
        <div v-if="mode === 'signature' && selectedSignature" class="pdf-tool-options">
          <label>透明度 <input :value="signatureOpacity" type="range" min="0.2" max="1" step="0.05" @pointerdown="beginOpacityEdit" @input="updateSignatureOpacity($event.target.value)" /></label>
          <label>大小 {{ Math.round(selectedSignature.width * 100) }}% <input :value="Math.round(selectedSignature.width * 100)" type="range" min="5" max="90" step="1" @pointerdown="beginSignatureTransform" @input="updateSignatureScale($event.target.value)" /></label>
          <label>旋转 {{ Math.round(selectedSignature.rotation || 0) }}° <input :value="selectedSignature.rotation || 0" type="range" min="-180" max="180" step="1" @pointerdown="beginSignatureTransform" @input="updateSignatureRotation($event.target.value)" /></label>
          <button type="button" class="pdf-tool-button" aria-label="逆时针旋转签名 90 度" @click="rotateSelectedSignature(-90)"><svg class="toolbar-icon pdf-control-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 8a9 9 0 1 1-1 7" /><path d="M4 3v5h5" /></svg><span>90°</span></button>
          <button type="button" class="pdf-tool-button" aria-label="顺时针旋转签名 90 度" @click="rotateSelectedSignature(90)"><svg class="toolbar-icon pdf-control-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M20 8a9 9 0 1 0 1 7" /><path d="M20 3v5h-5" /></svg><span>90°</span></button>
          <button type="button" class="pdf-tool-button pdf-remove-signature" @click="removeSelectedSignature">删除选中签名</button>
        </div>
        <div class="pdf-page-controls" role="group" aria-label="PDF 页面与缩放">
          <button type="button" class="pdf-tool-button" :disabled="pageIndex === 0 || loading" aria-label="上一页" @click="changePage(-1)"><svg class="toolbar-icon pdf-control-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m15 18-6-6 6-6" /></svg></button>
          <span>{{ pageCount ? `${pageIndex + 1} / ${pageCount} 页` : "读取中" }}</span>
          <button type="button" class="pdf-tool-button" :disabled="pageIndex >= pageCount - 1 || loading" aria-label="下一页" @click="changePage(1)"><svg class="toolbar-icon pdf-control-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m9 6 6 6-6 6" /></svg></button>
          <button type="button" class="pdf-tool-button pdf-zoom-button" :disabled="loading" aria-label="缩小" @click="changeZoom(-0.1)"><svg class="toolbar-icon pdf-control-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 12h14" /></svg></button>
          <span>{{ Math.round(zoom * 100) }}%</span>
          <button type="button" class="pdf-tool-button pdf-zoom-button" :disabled="loading" aria-label="放大" @click="changeZoom(0.1)"><svg class="toolbar-icon pdf-control-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 12h14M12 5v14" /></svg></button>
        </div>
      </div>
    </template>

    <div ref="stage" class="pdf-page-stage" :class="{ 'is-editing': mode !== 'select' }" :inert="exporting">
      <div class="pdf-page-stack" :class="{ 'is-page-hidden': !pageSize.width || errorMessage }" :style="{ width: `${pageSize.width}px`, height: `${pageSize.height}px` }">
        <canvas ref="pageCanvas" class="pdf-page-canvas" aria-label="PDF 页面预览"></canvas>
        <canvas
          ref="overlayCanvas"
          class="pdf-overlay-canvas"
          :class="{ 'is-eraser': mode === 'erase', 'is-signature': mode === 'signature' }"
          :aria-label="mode === 'erase' ? '拖动以擦除文字' : mode === 'signature' ? '拖动签名或调整签名大小' : 'PDF 页面编辑层'"
          @pointerdown="onOverlayPointerDown"
          @pointermove="onOverlayPointerMove"
          @pointerup="onOverlayPointerUp"
          @pointercancel="onOverlayPointerUp"
        ></canvas>
      </div>
      <div v-if="loading" class="pdf-loading"><span class="pdf-loading-spinner"></span>正在渲染 PDF 页面…</div>
    </div>

    <p v-if="errorMessage" class="pdf-editor-error" role="alert">{{ errorMessage }}</p>

    <div v-if="hasEdits" class="pdf-editor-footer">
      <span v-if="hasFlattenedEdits">擦除过的页面会压平为图像；原 PDF 批注和表单仍保留，擦除不会影响它们。仅添加签名的页面保留原 PDF 内容。保存副本会加入本地历史，原文件保留。</span>
      <span v-else>仅添加签名会保留原 PDF 页面、批注和表单。保存副本会加入本地历史，原文件保留。</span>
      <div>
        <button type="button" class="ghost-button" :disabled="exporting" @click="restoreOriginal">恢复原文件</button>
      </div>
    </div>
    <dialog v-if="drawDialog" ref="drawDialogElement" class="signature-dialog" aria-labelledby="signature-title" @cancel.prevent="closeDrawDialog" @click="onDrawDialogClick">
      <header><div><h3 id="signature-title">手写签名</h3><p>在下方空白区域签名，确认后即可放到 PDF 页面。</p></div><button ref="drawDialogCloseButton" type="button" class="icon-button" aria-label="关闭" @click="closeDrawDialog"><svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 6 12 12M18 6 6 18" /></svg></button></header>
      <canvas ref="drawCanvas" class="signature-pad" aria-label="手写签名区域" @pointerdown="drawPointerDown" @pointermove="drawPointerMove" @pointerup="endDrawPointer" @pointercancel="endDrawPointer"></canvas>
      <footer><label>笔迹 <input v-model.number="drawBrush" type="range" min="2" max="10" step="1" /></label><button type="button" class="ghost-button" @click="clearDrawCanvas">清空</button><button type="button" class="ghost-button" @click="closeDrawDialog">取消</button><button type="button" class="primary-button small-button" :disabled="!drawHasInk" @click="useDrawnSignature">使用签名</button></footer>
    </dialog>
  </DocumentWorkspace>
</template>

<style scoped>
.pdf-editor { overflow: hidden; border: 1px solid var(--line); border-radius: 16px; background: #fff; box-shadow: var(--shadow); }
.pdf-editor-toolbar, .pdf-editor-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 14px; border-bottom: 1px solid var(--line); }
.pdf-editor-footer > span { flex: 0 0 auto; color: var(--muted); font-size: 11px; }
.pdf-editor-actions, .pdf-tool-group, .pdf-page-controls, .pdf-tool-options, .pdf-editor-footer > div { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; }
.pdf-editor-actions .ghost-button { padding: 7px 10px; font-size: 11px; }
.pdf-editor-actions .document-workspace-close { margin-left: auto; }
.pdf-editor-toolbar { align-items: flex-start; justify-content: flex-start; flex-wrap: wrap; background: #fcfcff; }
.pdf-tool-button { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 34px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; color: #596174; background: #fff; font-size: 11px; }
.pdf-control-icon { width: 16px; height: 16px; flex: 0 0 16px; }
.pdf-tool-button:hover, .pdf-tool-button.is-active { border-color: #c9c7ff; color: var(--violet-dark); background: var(--violet-soft); }
.pdf-tool-button:disabled { opacity: .45; cursor: not-allowed; }
.pdf-tool-options { min-height: 34px; padding-left: 9px; border-left: 1px solid var(--line); color: var(--muted); font-size: 11px; }
.pdf-tool-options label, .signature-dialog footer label { display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }
.pdf-tool-options input[type="range"] { width: 92px; accent-color: var(--violet); }
.pdf-remove-signature { color: var(--danger); }
.pdf-confirm-erase { border-color: var(--violet); color: #fff; background: var(--violet); }
.pdf-confirm-erase:hover:not(:disabled) { border-color: var(--violet-dark); color: #fff; background: var(--violet-dark); }
.pdf-page-controls { margin-left: auto; gap: 5px; color: var(--muted); font-size: 11px; }
.pdf-page-controls > span { min-width: 48px; text-align: center; }
.pdf-zoom-button { min-width: 31px; padding-inline: 6px; font-size: 14px; }
.pdf-page-stage { position: relative; display: grid; flex: 1 1 auto; place-items: start center; min-height: min(64vh, 720px); max-height: 78vh; padding: 21px; overflow: auto; background: #eff1f7; }
.pdf-page-stack { position: relative; flex: 0 0 auto; margin: auto; box-shadow: 0 9px 28px rgba(24,30,54,.16); background: #fff; }
.pdf-page-canvas, .pdf-overlay-canvas { position: absolute; inset: 0; display: block; }
.pdf-overlay-canvas { touch-action: none; }
.pdf-overlay-canvas.is-eraser { cursor: crosshair; }
.pdf-overlay-canvas.is-signature { cursor: move; }
.pdf-overlay-canvas:not(.is-eraser):not(.is-signature) { pointer-events: none; }
.pdf-loading { position: absolute; z-index: 2; inset: 0; display: flex; align-items: center; justify-content: center; gap: 10px; min-height: 0; color: var(--muted); background: rgba(239,241,247,.72); font-size: 13px; }
.pdf-page-stack.is-page-hidden { visibility: hidden; }
.pdf-editor-error { margin: 0; padding: 9px 14px; color: var(--danger); font-size: 12px; }
.pdf-loading-spinner { width: 19px; height: 19px; border: 2px solid #dedfff; border-top-color: var(--violet); border-radius: 50%; animation: pdf-spin .8s linear infinite; }
@keyframes pdf-spin { to { transform: rotate(360deg); } }
.pdf-editor-footer { min-height: 56px; border-top: 1px solid var(--line); border-bottom: 0; }
.pdf-editor-footer .ghost-button { padding: 8px 10px; font-size: 11px; }
.pdf-editor-footer .primary-button { white-space: nowrap; }
dialog.signature-dialog { width: min(560px, calc(100vw - 36px)); max-height: calc(100vh - 36px); max-height: calc(100dvh - 36px); overflow: auto; padding: 18px; border: 1px solid var(--line); border-radius: 16px; color: var(--ink); background: #fff; box-shadow: var(--shadow); font: inherit; }
dialog.signature-dialog::backdrop { background: rgba(24,30,54,.35); backdrop-filter: blur(3px); }
.signature-dialog header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.signature-dialog header h3 { margin: 0 0 5px; font-size: 17px; }
.signature-dialog header p { margin: 0; color: var(--muted); font-size: 12px; }
.signature-pad { display: block; width: 100%; height: 210px; border: 1px dashed #cfd3df; border-radius: 11px; background: #fff; touch-action: none; }
.signature-dialog footer { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.signature-dialog footer label { margin-right: auto; color: var(--muted); font-size: 11px; }
.signature-dialog footer input { accent-color: var(--violet); }
@media (max-width: 760px) {
  .pdf-tool-button { min-height: 44px; }
  .pdf-page-controls > .pdf-tool-button { min-width: 44px; }
  .pdf-editor-actions .ghost-button,
  .pdf-editor-actions .primary-button,
  .pdf-editor-footer .ghost-button,
  .pdf-editor-footer .primary-button,
  .signature-dialog header .icon-button,
  .signature-dialog footer .ghost-button,
  .signature-dialog footer .primary-button { min-height: 44px; }
  .pdf-editor-actions .document-workspace-close,
  .signature-dialog header .icon-button { width: 44px; height: 44px; }
}
@media (any-pointer: coarse) {
  .pdf-tool-button { min-height: 44px; }
  .pdf-page-controls > .pdf-tool-button { min-width: 44px; }
  .pdf-editor-actions .ghost-button,
  .pdf-editor-actions .primary-button,
  .pdf-editor-footer .ghost-button,
  .pdf-editor-footer .primary-button,
  .signature-dialog header .icon-button,
  .signature-dialog footer .ghost-button,
  .signature-dialog footer .primary-button { min-height: 44px; }
}
@media (max-width: 700px) {
  .pdf-editor-toolbar { align-items: stretch; flex-direction: column; }
  .pdf-editor-actions { justify-content: flex-end; }
  .pdf-tool-group { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .pdf-tool-options { justify-content: space-between; padding: 8px 0 0; border-top: 1px solid var(--line); border-left: 0; }
  .pdf-tool-options input[type="range"] { width: min(30vw, 110px); }
  .pdf-page-controls { justify-content: center; margin: 0; }
  .pdf-page-stage { min-height: min(58vh, 600px); max-height: 68vh; padding: 12px; }
  .pdf-editor-footer { align-items: stretch; flex-direction: column; }
  .pdf-editor-footer > div { justify-content: flex-end; }
  .signature-pad { height: 180px; }
}
</style>
