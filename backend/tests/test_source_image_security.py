import asyncio
from io import BytesIO
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import main
from app.models import EditedDocumentRequest


class TemporaryStorage:
    def __init__(self, root: Path, download_payload: bytes | None = None):
        self.root = root
        self.download_payload = download_payload
        self._remote = None

    def path(self, name: str) -> Path:
        return self.root / name

    def download_published(self, name: str, destination: Path) -> None:
        if self.download_payload is None:
            raise FileNotFoundError(name)
        destination.write_bytes(self.download_payload)

    def publish(self, path: Path) -> str:
        return f"http://files.test/{path.name}"

    def published_url(self, name: str) -> str:
        return f"http://files.test/{Path(name).name}"


class SourceImageSecurityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request_for_host(host: str) -> Request:
        return Request({
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": (host, 80),
            "path": "/api/v1/documents/export-edited",
            "raw_path": b"/api/v1/documents/export-edited",
            "query_string": b"",
            "headers": [(b"host", host.encode("ascii"))],
            "client": ("127.0.0.1", 12345),
            "root_path": "",
        })

    async def test_export_rejects_positioned_words_outside_the_original_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_root = root / "metadata"
            metadata_root.mkdir()
            source_image = root / "source.png"
            with Image.new("RGB", (100, 50), "white") as image:
                image.save(source_image, format="PNG")
            (metadata_root / "known-document.json").write_text(
                json.dumps({
                    "paragraphs": ["原文"],
                    "source_image_file": source_image.name,
                    "image_width": 100,
                    "image_height": 50,
                }),
                encoding="utf-8",
            )
            storage = TemporaryStorage(root)

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda name: metadata_root / f"{name}.json"),
                patch.object(main, "create_docx") as create_docx,
            ):
                with self.assertRaises(HTTPException) as context:
                    await main.export_edited_document(
                        self.request_for_host("api.example.test"),
                        EditedDocumentRequest(
                            document_id="known-document",
                            positioned_words=[{
                                "text": "越界",
                                "left": 98,
                                "top": 10,
                                "width": 6,
                                "height": 8,
                            }],
                        ),
                    )

            self.assertEqual(context.exception.status_code, 422)
            create_docx.assert_not_called()

    async def test_export_never_fetches_a_metadata_image_url_over_http(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_root = root / "metadata"
            metadata_root.mkdir()
            (metadata_root / "known-document.json").write_text(
                json.dumps({
                    "paragraphs": ["原文"],
                    "source_image_file": "missing-source.jpg",
                    "source_image_url": "http://169.254.169.254/latest/meta-data/",
                    "warnings": ["云端识别暂时失败，已使用本机 OCR；请校对识别文字和表格"],
                }),
                encoding="utf-8",
            )
            storage = TemporaryStorage(root)

            def write_docx(_result, output_path, *_args, **_kwargs):
                Path(output_path).write_bytes(b"docx")

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda name: metadata_root / f"{name}.json"),
                patch.object(main, "create_docx", side_effect=write_docx),
                patch.object(main, "convert_docx_to_pdf", return_value=(None, False)),
                patch.object(main.httpx, "get", side_effect=httpx.ConnectError("blocked in test")) as fetch,
            ):
                response = await main.export_edited_document(
                    self.request_for_host("api.example.test"),
                    EditedDocumentRequest(
                        document_id="known-document",
                        paragraphs=["修改后的内容"],
                    )
                )

            self.assertEqual(response.paragraphs, ["修改后的内容"])
            self.assertIsNone(response.source_image_url)
            self.assertIn("已使用本机 OCR", "；".join(response.warnings))
            saved_metadata = json.loads(
                (metadata_root / f"{response.id}.json").read_text(encoding="utf-8")
            )
            self.assertIn("已使用本机 OCR", "；".join(saved_metadata["warnings"]))
            fetch.assert_not_called()

    def test_metadata_path_rejects_ids_that_escape_the_metadata_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = SimpleNamespace(root=Path(directory) / "storage")
            external_json_path = Path(directory) / "outside.json"

            with patch.object(main, "storage", storage):
                with self.assertRaises(HTTPException) as raised:
                    main.metadata_path(str(external_json_path.with_suffix("")))
                valid_path = main.metadata_path("a" * 32)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(valid_path.parent, storage.root.parent / "storage_metadata")
        self.assertEqual(valid_path.name, f"{'a' * 32}.json")

    async def test_export_rebuilds_image_url_instead_of_propagating_metadata_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_root = root / "metadata"
            metadata_root.mkdir()
            (metadata_root / "known-document.json").write_text(
                json.dumps({
                    "paragraphs": ["原文"],
                    "source_image_file": "source.jpg",
                    "source_image_url": "http://169.254.169.254/latest/meta-data/",
                    "image_width": 100,
                    "image_height": 100,
                }),
                encoding="utf-8",
            )
            storage = TemporaryStorage(root, download_payload=b"stored image")

            def write_docx(_result, output_path, *_args, **_kwargs):
                Path(output_path).write_bytes(b"docx")

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda name: metadata_root / f"{name}.json"),
                patch.object(main, "create_docx", side_effect=write_docx),
                patch.object(main, "convert_docx_to_pdf", return_value=(None, False)),
                patch.object(main.httpx, "get", side_effect=httpx.ConnectError("blocked in test")) as fetch,
            ):
                response = await main.export_edited_document(
                    self.request_for_host("api.example.test"),
                    EditedDocumentRequest(
                        document_id="known-document",
                        paragraphs=["修改后的内容"],
                    ),
                )

            self.assertTrue(response.source_image_url.startswith("http://api.example.test/"))
            self.assertNotIn("169.254.169.254", response.source_image_url)
            saved_metadata = json.loads((metadata_root / f"{response.id}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_metadata["source_image_url"], response.source_image_url)
            self.assertEqual(saved_metadata["source_image_file"], "source.jpg")
            fetch.assert_not_called()

    async def test_export_downloads_a_remote_png_to_a_png_suffixed_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_root = root / "metadata"
            metadata_root.mkdir()
            (metadata_root / "known-document.json").write_text(
                json.dumps({
                    "paragraphs": ["原文"],
                    "source_image_file": "source.png",
                    "image_width": 2,
                    "image_height": 2,
                }),
                encoding="utf-8",
            )
            image_bytes = BytesIO()
            Image.new("RGB", (2, 2), "white").save(image_bytes, format="PNG")
            storage = TemporaryStorage(root, download_payload=image_bytes.getvalue())
            captured_source = {}

            def write_docx(_result, output_path, source_path, **_kwargs):
                captured_source["path"] = Path(source_path)
                with Image.open(source_path) as image:
                    captured_source["format"] = image.format
                Path(output_path).write_bytes(b"docx")

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda name: metadata_root / f"{name}.json"),
                patch.object(main, "create_docx", side_effect=write_docx),
                patch.object(main, "convert_docx_to_pdf", return_value=(None, False)),
            ):
                await main.export_edited_document(
                    self.request_for_host("api.example.test"),
                    EditedDocumentRequest(
                        document_id="known-document",
                        paragraphs=["修改后的内容"],
                    ),
                )

            self.assertTrue(captured_source["path"].name.endswith("_source.png"))
            self.assertEqual(captured_source["format"], "PNG")
            self.assertFalse(captured_source["path"].exists(), "temporary source image should be removed")

    async def test_repeated_edits_keep_original_image_and_ocr_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_root = root / "metadata"
            metadata_root.mkdir()
            source_image = root / "source.jpg"
            source_image.write_bytes(b"original scan")
            original_word = {
                "text": "原文标题",
                "left": 15.0,
                "top": 20.0,
                "width": 50.0,
                "height": 12.0,
            }
            original_cell_word = {
                "text": "原单元格",
                "left": 5.0,
                "top": 55.0,
                "width": 40.0,
                "height": 12.0,
            }
            original_cell = {
                "row_start": 0,
                "row_end": 1,
                "col_start": 0,
                "col_end": 1,
                "text": "原单元格",
                "left": 0.0,
                "top": 50.0,
                "right": 100.0,
                "bottom": 80.0,
            }
            (metadata_root / "source-document.json").write_text(
                json.dumps({
                    "paragraphs": ["旧保存标题"],
                    "paragraph_styles": ["title"],
                    "tables": [[["旧保存单元格"]]],
                    "table_cells": [[{**original_cell, "text": "旧保存单元格"}]],
                    "positioned_words": [
                        {**original_word, "text": "旧保存标题"},
                        {**original_cell_word, "text": "旧保存单元格"},
                    ],
                    "source_positioned_words": [original_word, original_cell_word],
                    "source_image_file": source_image.name,
                    "source_paper_colors": {
                        "word:0": "#b2b5b8",
                        "word:1": "#b2b5b8",
                        "cell:0:0:0": "#b2b5b8",
                    },
                    "image_width": 100,
                    "image_height": 100,
                }),
                encoding="utf-8",
            )
            storage = TemporaryStorage(root)
            docx_originals = []
            image_originals = []

            def write_docx(_result, output_path, *_args, original_words=None, **_kwargs):
                docx_originals.append(original_words)
                Path(output_path).write_bytes(b"docx")

            def write_image(_result, _source, output_path, *, original_words=None):
                image_originals.append(original_words)
                Path(output_path).write_bytes(b"png")

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda name: metadata_root / f"{name}.json"),
                patch.object(main, "create_docx", side_effect=write_docx),
                patch.object(
                    main,
                    "create_hybrid_editable_docx",
                    side_effect=lambda _result, output_path, *_args: Path(output_path).write_bytes(b"structured docx"),
                ),
                patch.object(main, "render_positioned_image", side_effect=write_image),
                patch.object(main, "convert_docx_to_pdf", return_value=(None, False)),
                patch.object(main, "asdict", wraps=main.asdict) as serialized_dataclass,
            ):
                first = await main.export_edited_document(
                    self.request_for_host("api.example.test"),
                    EditedDocumentRequest(
                        document_id="source-document",
                        paragraphs=["第一次修改"],
                        positioned_words=[
                            {**original_word, "text": "第一次修改"},
                            {**original_cell_word, "text": "第一次单元格"},
                        ],
                        tables=[[["第一次单元格"]]],
                    ),
                )
                second = await main.export_edited_document(
                    self.request_for_host("api.example.test"),
                    EditedDocumentRequest(
                        document_id=first.id,
                        paragraphs=["第二次修改"],
                        positioned_words=[
                            {**first.positioned_words[0], "text": "第二次修改"},
                            {**first.positioned_words[1], "text": "第二次单元格"},
                        ],
                        tables=[[["第二次单元格"]]],
                    ),
                )

            serialized_types = [type(call.args[0]) for call in serialized_dataclass.call_args_list]
            self.assertEqual(serialized_types.count(main.PositionedWord), 8)
            self.assertEqual(serialized_types.count(main.TableCell), 4)
            first_metadata = json.loads(
                (metadata_root / f"{first.id}.json").read_text(encoding="utf-8")
            )
            second_metadata = json.loads(
                (metadata_root / f"{second.id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first_metadata["source_image_file"], source_image.name)
            self.assertEqual(second_metadata["source_image_file"], source_image.name)
            self.assertEqual(first_metadata["source_paper_colors"]["word:0"], "#b2b5b8")
            self.assertEqual(second_metadata["source_paper_colors"], first_metadata["source_paper_colors"])
            self.assertIn("--paper-color:#b2b5b8", second.editor_html)
            self.assertEqual(
                first_metadata["source_positioned_words"],
                [original_word, original_cell_word],
            )
            self.assertEqual(
                second_metadata["source_positioned_words"],
                [original_word, original_cell_word],
            )
            self.assertEqual(first_metadata["source_table_cells"], [[original_cell]])
            self.assertEqual(second_metadata["source_table_cells"], [[original_cell]])
            self.assertEqual(first_metadata["table_cells"][0][0]["text"], "第一次单元格")
            self.assertEqual(second_metadata["table_cells"][0][0]["text"], "第二次单元格")
            self.assertIn("data-edited='1'", first.editor_html)
            self.assertIn("data-edited='1'", second.editor_html)
            self.assertEqual(second_metadata["positioned_words"][0]["text"], "第二次修改")
            self.assertEqual(second_metadata["positioned_words"][1]["text"], "第二次单元格")
            self.assertEqual(len(docx_originals), 2)
            # When LibreOffice is unavailable, table exports render the
            # edited text over the source scan for an original-layout PDF.
            self.assertEqual(len(image_originals), 2)
            for baseline in docx_originals:
                self.assertEqual(
                    [word.text for word in baseline],
                    [original_word["text"], original_cell_word["text"]],
                )

    def test_refresh_reopens_saved_coordinate_edits_over_the_original_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_root = root / "metadata"
            metadata_root.mkdir()
            source_image = root / "source.png"
            Image.new("RGB", (100, 100), (178, 181, 184)).save(source_image, format="PNG")
            original_word = {
                "text": "原文标题",
                "left": 10.0,
                "top": 10.0,
                "width": 30.0,
                "height": 10.0,
            }
            original_cell_word = {
                "text": "原单元格",
                "left": 5.0,
                "top": 50.0,
                "width": 30.0,
                "height": 10.0,
            }
            original_emptying_cell_word = {
                "text": "待清空",
                "left": 55.0,
                "top": 50.0,
                "width": 30.0,
                "height": 10.0,
            }
            original_cell = {
                "row_start": 0,
                "row_end": 1,
                "col_start": 0,
                "col_end": 1,
                "text": "原单元格",
                "left": 0.0,
                "top": 40.0,
                "right": 50.0,
                "bottom": 80.0,
            }
            original_emptying_cell = {
                "row_start": 0,
                "row_end": 1,
                "col_start": 1,
                "col_end": 2,
                "text": "待清空",
                "left": 50.0,
                "top": 40.0,
                "right": 100.0,
                "bottom": 80.0,
            }
            metadata = {
                "image_width": 100,
                "image_height": 100,
                "warnings": ["云端识别暂时失败，已使用本机 OCR；请校对识别文字和表格"],
                "paragraphs": ["新标题"],
                "paragraph_styles": ["title"],
                "paragraph_layouts": [],
                "positioned_words": [
                    {**original_word, "text": "新标题"},
                    {**original_cell_word, "text": "新单元格"},
                    {**original_emptying_cell_word, "text": ""},
                ],
                "source_positioned_words": [
                    original_word,
                    original_cell_word,
                    original_emptying_cell_word,
                ],
                "source_image_file": source_image.name,
                "tables": [[["新单元格", ""]]],
                "table_layouts": [],
                "table_cells": [[
                    {**original_cell, "text": "新单元格"},
                    {**original_emptying_cell, "text": ""},
                ]],
                "docx_file": "source.docx",
                "structured_docx_file": "source-structured.docx",
                "pdf_file": "source.pdf",
                # Simulate opening an older document after a visual-layout
                # upgrade. The backfill must retain edits made since OCR.
                "visual_docx_layout_version": str(int(main.VISUAL_DOCX_LAYOUT_VERSION) - 1),
                "structured_docx_layout_version": main.STRUCTURED_DOCX_LAYOUT_VERSION,
            }
            document_id = "a" * 32
            (metadata_root / f"{document_id}.json").write_text(
                json.dumps(metadata, ensure_ascii=False),
                encoding="utf-8",
            )
            storage = TemporaryStorage(root)
            rebuilt_visual_words = []

            def capture_visual_rebuild(result, destination, source, original_words=None, **_kwargs):
                rebuilt_visual_words.append(original_words)
                destination.write_bytes(b"rebuilt visual document")

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda _name: metadata_root / f"{document_id}.json"),
                patch.object(main, "create_docx", side_effect=capture_visual_rebuild),
            ):
                refreshed = main.refresh_document(
                    self.request_for_host("api.example.test"),
                    document_id,
                )

            self.assertEqual(refreshed.positioned_words[0]["text"], "新标题")
            self.assertIn("已使用本机 OCR", "；".join(refreshed.warnings))
            self.assertEqual(
                [[word.text for word in words] for words in rebuilt_visual_words],
                [["原文标题", "原单元格", "待清空"]],
            )
            self.assertIn(".coordinate-word[data-edited='1']", refreshed.editor_html)
            self.assertIn(".coordinate-cell[data-edited='1']", refreshed.editor_html)
            refreshed_metadata = json.loads(
                (metadata_root / f"{document_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(refreshed_metadata["source_paper_colors"]["word:0"], "#b2b5b8")
            self.assertEqual(
                refreshed_metadata["visual_docx_layout_version"],
                main.VISUAL_DOCX_LAYOUT_VERSION,
            )
            self.assertIn("--paper-color:#b2b5b8", refreshed.editor_html)
            self.assertEqual(
                refreshed.editor_html.count("contenteditable='true' data-edited='1'"),
                3,
            )

    def test_refresh_generates_separate_visual_and_searchable_table_pdfs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_root = root / "metadata"
            metadata_root.mkdir()
            source_image = root / "source.png"
            Image.new("RGB", (100, 150), "white").save(source_image)
            (root / "old.docx").write_bytes(b"visual docx")
            (root / "old_structured.docx").write_bytes(b"structured docx")
            metadata_file = metadata_root / "old-document.json"
            metadata_file.write_text(
                json.dumps({
                    "title": "表格",
                    "image_width": 100,
                    "image_height": 150,
                    "paragraphs": [],
                    "tables": [[["识别内容"]]],
                    "table_layouts": [],
                    "table_cells": [],
                    "source_table_cells": [],
                    "positioned_words": [],
                    "source_positioned_words": [],
                    "source_image_file": source_image.name,
                    "docx_file": "old.docx",
                    "visual_docx_layout_version": main.VISUAL_DOCX_LAYOUT_VERSION,
                    "structured_docx_file": "old_structured.docx",
                    "structured_docx_layout_version": main.STRUCTURED_DOCX_LAYOUT_VERSION,
                    # Simulate the old single PDF that was rendered from the
                    # native table and did not preserve the scan layout.
                    "pdf_file": "old_structured.pdf",
                    "structured_pdf_layout_version": "16",
                }),
                encoding="utf-8",
            )
            storage = TemporaryStorage(root)
            calls = []

            def render_image(_result, _source, output_path, *, original_words=None):
                Path(output_path).write_bytes(b"scan image")

            def convert_pdf(docx_path, _output_dir, **kwargs):
                calls.append((Path(docx_path).name, kwargs))
                output = root / f"{Path(docx_path).stem}.pdf"
                output.write_bytes(b"%PDF-1.7")
                return output, bool(kwargs.get("prefer_image"))

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda _name: metadata_file),
                patch.object(main, "render_positioned_image", side_effect=render_image),
                patch.object(main, "convert_docx_to_pdf", side_effect=convert_pdf),
            ):
                response = main.refresh_document(
                    self.request_for_host("api.example.test"),
                    "old-document",
                )
                structured_download = main.download_document_file(
                    "old-document",
                    "structured-pdf",
                )

            self.assertEqual(response.pdf_url.split("/download/")[1].split("?")[0], "pdf")
            self.assertEqual(
                response.structured_pdf_url.split("/download/")[1].split("?")[0],
                "structured-pdf",
            )
            self.assertEqual([name for name, _kwargs in calls], ["old.docx", "old_structured.docx"])
            self.assertTrue(calls[0][1]["prefer_image"])
            self.assertNotIn("fallback_image_path", calls[1][1])
            self.assertEqual(structured_download.media_type, "application/pdf")
            refreshed = json.loads(metadata_file.read_text(encoding="utf-8"))
            expected_name = main._document_download_filename(
                "old-document",
                "structured-pdf",
                refreshed,
            )
            self.assertIn(quote(expected_name), structured_download.headers["content-disposition"])
            self.assertEqual(refreshed["pdf_file"], "old.pdf")
            self.assertEqual(refreshed["structured_pdf_file"], "old_structured.pdf")
            self.assertEqual(refreshed["visual_pdf_layout_version"], main.VISUAL_PDF_LAYOUT_VERSION)
            self.assertEqual(refreshed["structured_pdf_layout_version"], main.TABLE_PDF_LAYOUT_VERSION)


if __name__ == "__main__":
    unittest.main()
