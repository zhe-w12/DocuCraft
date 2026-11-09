import test from "node:test";
import assert from "node:assert/strict";
import { PDFDict, PDFDocument, PDFName, StandardFonts } from "pdf-lib";
import { applyPdfAnnotations } from "../src/pdf-export.js";

const onePixelPng = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

test("flattens edited pages to remove underlying content and keeps untouched pages intact", async () => {
  const original = await PDFDocument.create();
  const originalPage = original.addPage([600, 800]);
  const font = await original.embedFont(StandardFonts.Helvetica);
  originalPage.drawText("redacted text", { font });
  const untouchedPage = original.addPage([400, 500]);
  untouchedPage.drawText("keep vector text", { font });
  const sourceBytes = await original.save();
  const states = new Map([[0, {
    strokes: [{ width: 0.02, points: [{ x: 0.2, y: 0.3 }, { x: 0.3, y: 0.35 }] }],
    signatures: [{
      opacity: 1,
    }],
  }]]);

  const editedBytes = await applyPdfAnnotations(sourceBytes, states, async (pageIndex, state) => {
    assert.equal(pageIndex, 0);
    assert.equal(state.strokes.length, 1);
    return Buffer.from(onePixelPng.split(",")[1], "base64");
  });
  const edited = await PDFDocument.load(editedBytes);
  assert.equal(edited.getPageCount(), 2);
  assert.deepEqual(edited.getPage(0).getSize(), { width: 600, height: 800 });
  const resources = edited.getPage(0).node.Resources();
  const xObjects = resources.lookup(PDFName.of("XObject"), PDFDict);
  assert.equal(xObjects.keys().length, 1, "the edited page should be flattened into an image");
  const flattenedFonts = resources.lookupMaybe(PDFName.of("Font"), PDFDict);
  assert.equal(flattenedFonts?.keys().length || 0, 0, "the covered source text must not remain on the edited page");
  const untouchedFonts = edited.getPage(1).node.Resources().lookup(PDFName.of("Font"), PDFDict);
  assert.ok(untouchedFonts.keys().length > 0, "an unedited page should retain its vector text resources");
  assert.deepEqual(edited.getPage(1).getSize(), { width: 400, height: 500 });
});
