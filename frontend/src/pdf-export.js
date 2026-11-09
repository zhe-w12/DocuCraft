export async function applyPdfAnnotations(
  pdfBytes,
  pageStates,
  renderEditedPage,
  renderSignatureOverlay = null,
) {
  const { PDFDocument, PDFName, PDFArray, PDFDict, PDFRef, PDFStream } = await import("pdf-lib");
  const source = await PDFDocument.load(pdfBytes);
  const sourcePages = source.getPages();

  let hasEdits = false;
  for (let index = 0; index < sourcePages.length; index += 1) {
    const state = pageStates.get(index);
    if (!state || (!state.strokes.length && !state.signatures.length)) {
      continue;
    }
    hasEdits = true;
    const page = sourcePages[index];
    const signatureOnly = !state.strokes.length && state.signatures.length;
    if (signatureOnly && typeof renderSignatureOverlay === "function") {
      const imageBytes = await renderSignatureOverlay(index, state);
      const image = await source.embedPng(imageBytes);
      const bounds = getVisiblePageBounds(page);
      const pageResources = clonePageResources(
        page,
        source.context,
        PDFName,
        PDFDict,
      );
      page.node.set(PDFName.of("Resources"), pageResources);
      page.drawImage(image, {
        x: bounds.x,
        y: bounds.y,
        width: bounds.width,
        height: bounds.height,
      });
      continue;
    }
    if (typeof renderEditedPage !== "function") throw new Error("需要渲染已编辑页面才能导出");
    const imageBytes = await renderEditedPage(index, state);
    const isJpeg = imageBytes[0] === 0xff && imageBytes[1] === 0xd8;
    const image = isJpeg
      ? await source.embedJpg(imageBytes)
      : await source.embedPng(imageBytes);
    const bounds = getVisiblePageBounds(page);

    // Keep the source page object and its annotations in place. Replacing only
    // its content preserves document-level navigation and interactive fields.
    page.node.delete(PDFName.of("Contents"));
    const pageResources = page.node.Annots()?.size()
      ? clonePageResources(page, source.context, PDFName, PDFDict)
      : source.context.obj({});
    page.node.set(PDFName.of("Resources"), pageResources);
    page.drawImage(image, {
      x: bounds.x,
      y: bounds.y,
      width: bounds.width,
      height: bounds.height,
    });
  }
  if (!hasEdits) return pdfBytes;
  source.setModificationDate(new Date());
  pruneUnreachableObjects(source, { PDFArray, PDFDict, PDFRef, PDFStream });
  return source.save();
}

function getVisiblePageBounds(page) {
  const media = page.getMediaBox();
  const crop = page.getCropBox();
  const mediaLeft = Math.min(media.x, media.x + media.width);
  const mediaBottom = Math.min(media.y, media.y + media.height);
  const mediaRight = Math.max(media.x, media.x + media.width);
  const mediaTop = Math.max(media.y, media.y + media.height);
  const cropLeft = Math.min(crop.x, crop.x + crop.width);
  const cropBottom = Math.min(crop.y, crop.y + crop.height);
  const cropRight = Math.max(crop.x, crop.x + crop.width);
  const cropTop = Math.max(crop.y, crop.y + crop.height);
  const left = Math.max(mediaLeft, cropLeft);
  const bottom = Math.max(mediaBottom, cropBottom);
  const right = Math.min(mediaRight, cropRight);
  const top = Math.min(mediaTop, cropTop);
  if (right <= left || top <= bottom) {
    return {
      x: mediaLeft,
      y: mediaBottom,
      width: mediaRight - mediaLeft,
      height: mediaTop - mediaBottom,
    };
  }
  return { x: left, y: bottom, width: right - left, height: top - bottom };
}

function clonePageResources(page, context, PDFName, PDFDict) {
  const resources = page.node.Resources()?.clone(context) || context.obj({});
  // pdf-lib adds image and optional graphics state entries to these nested
  // dictionaries. Clone them too so pages that shared source resources remain
  // independent after the edit.
  for (const category of ["Font", "XObject", "ExtGState"]) {
    const key = PDFName.of(category);
    const dictionary = resources.lookupMaybe(key, PDFDict);
    if (dictionary) resources.set(key, dictionary.clone(context));
  }
  return resources;
}

function pruneUnreachableObjects(document, { PDFArray, PDFDict, PDFRef, PDFStream }) {
  const context = document.context;
  const reachableRefs = new Set();
  const visitedObjects = new Set();
  const pending = Object.values(context.trailerInfo).filter(Boolean);

  while (pending.length) {
    let object = pending.pop();
    if (object instanceof PDFRef) {
      const key = object.toString();
      if (reachableRefs.has(key)) continue;
      reachableRefs.add(key);
      object = context.lookup(object);
      if (!object) continue;
    }
    if (visitedObjects.has(object)) continue;
    visitedObjects.add(object);
    if (object instanceof PDFArray) {
      for (const value of object.asArray()) pending.push(value);
    } else if (object instanceof PDFDict) {
      for (const [, value] of object.entries()) pending.push(value);
    } else if (object instanceof PDFStream) {
      pending.push(object.dict);
    }
  }

  for (const [ref] of context.enumerateIndirectObjects()) {
    if (!reachableRefs.has(ref.toString())) context.delete(ref);
  }
}
