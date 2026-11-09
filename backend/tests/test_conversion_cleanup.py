import io
import json
import sys
import tempfile
import unittest
from uuid import uuid4
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import main
from app.services.ocr import (
    OcrResult,
    ParagraphLayout,
    PositionedWord,
    TableCell,
    TableLayout,
    TextFragment,
    _should_replace_tesseract_candidate,
    _write_table_line_removed_ocr_candidate,
)
from app.services.ocr import recognize_locally


class FakeStorage:
    def __init__(self, root: Path, *, fail_on: str | None = None):
        self.root = root
        self.fail_on = fail_on
        self.published: set[str] = set()
        self.objects: dict[str, bytes] = {}

    def path(self, name: str) -> Path:
        return self.root / name

    def publish(self, path: Path, *, inline: bool = False) -> str:
        self.published.add(path.name)
        self.objects[path.name] = path.read_bytes()
        path.unlink(missing_ok=True)
        if path.name.endswith(self.fail_on or "\0"):
            raise RuntimeError("simulated object upload error")
        return f"https://files.example/{path.name}"

    def delete_published(self, name: str) -> None:
        self.published.discard(Path(name).name)
        self.objects.pop(Path(name).name, None)
        self.path(name).unlink(missing_ok=True)


class LocalOcrCandidateTests(unittest.TestCase):
    def test_grid_candidate_can_trade_a_small_confidence_drop_for_more_text(self):
        current = [(90.0, "x" * 100)]
        candidate = [(80.0, "x" * 122)]

        self.assertTrue(
            _should_replace_tesseract_candidate(
                candidate,
                current,
                minimum_quality_ratio=0.95,
                minimum_coverage_ratio=0.9,
            )
        )

    def test_grid_candidate_is_rejected_when_it_drops_too_much_text(self):
        current = [(90.0, "x" * 100)]
        candidate = [(100.0, "x" * 85)]

        self.assertFalse(
            _should_replace_tesseract_candidate(
                candidate,
                current,
                minimum_quality_ratio=0.95,
                minimum_coverage_ratio=0.9,
            )
        )

    def test_table_line_candidate_removes_grid_rules_without_changing_size(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "table.png"
            candidate_path = Path(directory) / "table-without-lines.png"
            image = Image.new("L", (600, 400), "white")
            from PIL import ImageDraw

            draw = ImageDraw.Draw(image)
            for y in (20, 110, 200, 290, 380):
                draw.line((20, y, 580, y), fill="black", width=3)
            for x in (20, 220, 420, 580):
                draw.line((x, 20, x, 380), fill="black", width=3)
            draw.line((70, 60, 125, 60), fill="black", width=2)
            draw.line((70, 65, 70, 82), fill="black", width=2)
            image.save(image_path)

            self.assertTrue(
                _write_table_line_removed_ocr_candidate(image_path, candidate_path)
            )

            with Image.open(candidate_path) as candidate:
                self.assertEqual(candidate.size, image.size)
                self.assertGreater(candidate.getpixel((150, 20)), 180)
                self.assertLess(candidate.getpixel((90, 60)), 80)

    def test_table_line_candidate_skips_pages_without_a_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "plain.png"
            candidate_path = Path(directory) / "plain-without-lines.png"
            image = Image.new("L", (600, 400), "white")
            from PIL import ImageDraw

            ImageDraw.Draw(image).line((50, 80, 130, 80), fill="black", width=2)
            image.save(image_path)

            self.assertFalse(
                _write_table_line_removed_ocr_candidate(image_path, candidate_path)
            )
            self.assertFalse(candidate_path.exists())

    def test_local_ocr_uses_single_sparse_pass_for_grid_candidate(self):
        baseline_word = (
            "基线文字",
            {"left": 25, "top": 40, "width": 80, "height": 20},
        )
        grid_word = (
            "格线候选文字",
            {"left": 25, "top": 40, "width": 120, "height": 20},
        )

        def recognize_sparse(
            path,
            *,
            low_confidence_words=None,
            word_confidences=None,
            **_kwargs,
        ):
            if path.name != "scan.png":
                return []
            if low_confidence_words is not None:
                low_confidence_words.append((60.0, baseline_word[0]))
            if word_confidences is not None:
                word_confidences.append((60.0, baseline_word[0]))
            return [baseline_word]

        def recognize_direct(
            path,
            *,
            page_segmentation_mode,
            low_confidence_words=None,
            word_confidences=None,
            **_kwargs,
        ):
            if path.name != "table-lines-removed.png":
                return []
            if word_confidences is not None:
                word_confidences.append((58.0, grid_word[0]))
            return [grid_word] if page_segmentation_mode == 11 else []

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            image = Image.new("L", (600, 400), "white")
            from PIL import ImageDraw

            draw = ImageDraw.Draw(image)
            for y in (20, 110, 200, 290, 380):
                draw.line((20, y, 580, y), fill="black", width=3)
            for x in (20, 220, 420, 580):
                draw.line((x, 20, x, 380), fill="black", width=3)
            image.save(image_path)

            with (
                patch(
                    "app.services.ocr._tesseract_orientation_correction",
                    return_value=0,
                ),
                patch(
                    "app.services.ocr._tesseract_positioned_words_with_sparse_fallback",
                    side_effect=recognize_sparse,
                ) as sparse,
                patch(
                    "app.services.ocr._tesseract_positioned_words",
                    side_effect=recognize_direct,
                ) as direct,
                patch("app.services.ocr._table_from_image_lines", return_value=None),
            ):
                result = recognize_locally(image_path)

        self.assertEqual(result.paragraphs, [grid_word[0]])
        self.assertEqual(sparse.call_count, 4)
        direct.assert_called_once()
        self.assertEqual(direct.call_args.kwargs["page_segmentation_mode"], 11)


def png_image(width: int = 32, height: int = 24) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


class ConversionCleanupTests(unittest.TestCase):
    def test_retry_after_http_date_is_normalized_to_seconds(self):
        request = httpx.Request("POST", "https://ocr.example/recognize")
        response = httpx.Response(
            503,
            request=request,
            headers={"Retry-After": "Thu, 01 Jan 1970 00:00:05 GMT"},
        )
        error = httpx.HTTPStatusError("upstream failure", request=request, response=response)

        with patch.object(main.time, "time", return_value=0):
            self.assertEqual(main._retry_after_seconds(error), 5)

    def test_native_table_edit_keeps_the_source_cell_alignment(self):
        from app.services.documents import _is_centered_table_word

        cell = TableCell(0, 1, 0, 1, "左对齐内容", 0, 0, 100, 40)
        source_words = [PositionedWord("左对齐内容", 3, 10, 20, 12)]
        edited_words = main._apply_table_cell_edits(
            source_words,
            [(cell, "编辑后的长选项内容")],
        )
        result = OcrResult(
            tables=[[['编辑后的长选项内容']]],
            table_cells=[[cell]],
        )

        self.assertEqual(edited_words[0].left, source_words[0].left)
        self.assertGreater(edited_words[0].width, source_words[0].width)
        self.assertFalse(
            _is_centered_table_word(
                result,
                edited_words[0],
                original_words=source_words,
            )
        )

    def test_table_text_stays_synced_with_positioned_word_edits_after_reopen(self):
        cell = TableCell(0, 1, 0, 1, "School Name", 0, 0, 100, 30)
        source_words = [
            PositionedWord("School", 10, 8, 25, 10),
            PositionedWord("Name", 40, 8, 20, 10),
        ]
        edited_words = [
            source_words[0],
            PositionedWord("Title", 40, 8, 20, 10),
        ]

        tables = main._sync_table_text_from_positioned_words(
            [[["School Name"]]],
            [[cell]],
            source_words,
            edited_words,
        )
        self.assertEqual(tables, [[["School Title"]]])

        from app.services.editor import render_editor_html

        reopened_html = render_editor_html(
            OcrResult(
                image_width=100,
                image_height=50,
                positioned_words=edited_words,
                tables=tables,
                table_cells=[[TableCell(0, 1, 0, 1, tables[0][0][0], 0, 0, 100, 30)]],
            ),
            "http://example.test/source-image",
            source_positioned_words=source_words,
            source_table_cells=[[cell]],
            positioned_table_words=True,
        )

        self.assertIn("data-positioned-word='1' data-word-index='1'", reopened_html)
        self.assertNotIn("data-coordinate-cell='1'", reopened_html)

    def test_browser_measured_word_width_survives_save_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            metadata_root.mkdir()
            storage = FakeStorage(root)
            document_id = uuid4().hex
            source_name = f"{document_id}_source.png"
            Image.new("RGB", (120, 60), "white").save(root / source_name)
            source_word = {
                "text": "abcdefghij",
                "left": 20,
                "top": 10,
                "width": 20,
                "height": 16,
            }
            (metadata_root / f"{document_id}.json").write_text(
                json.dumps(
                    {
                        "image_width": 120,
                        "image_height": 60,
                        "paragraphs": ["abcdefghij"],
                        "paragraph_styles": ["body"],
                        "positioned_words": [source_word],
                        "source_positioned_words": [source_word],
                        "tables": [],
                        "table_layouts": [],
                        "table_cells": [],
                        "source_table_cells": [],
                        "source_image_file": source_name,
                    }
                ),
                encoding="utf-8",
            )
            captured = {}

            def write_docx(result, destination, *_args, **_kwargs):
                captured["words"] = result.positioned_words
                Path(destination).write_bytes(b"docx")

            def write_preview(_result, _source, destination, **_kwargs):
                Image.new("RGB", (120, 60), "white").save(destination)

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda value: metadata_root / f"{value}.json"),
                patch.object(main, "create_docx", side_effect=write_docx),
                patch.object(main, "render_positioned_image", side_effect=write_preview),
                patch.object(main, "convert_docx_to_pdf", return_value=(None, False)),
                TestClient(main.app) as client,
            ):
                response = client.post(
                    "/api/v1/documents/export-edited",
                    json={
                        "document_id": document_id,
                        "paragraphs": ["MMMMMMMMMMW"],
                        "positioned_words": [
                            {
                                "source_index": 0,
                                "text": "MMMMMMMMMMW",
                                "left": 20,
                                "top": 10,
                                "width": 20,
                                "height": 42,
                                "rendered_width": 58,
                            }
                        ],
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["positioned_words"][0]["width"], 58)
            self.assertEqual(body["positioned_words"][0]["height"], 42)
            self.assertIn("data-width='58.00'", body["editor_html"])
            self.assertIn("data-height='42.00'", body["editor_html"])
            self.assertEqual(captured["words"][0].width, 58)
            self.assertEqual(captured["words"][0].height, 42)
            saved_metadata = json.loads(
                next(path for path in metadata_root.glob("*.json") if path.name != f"{document_id}.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(saved_metadata["positioned_words"][0]["width"], 58)
            self.assertEqual(saved_metadata["positioned_words"][0]["height"], 42)

    def test_centered_heading_edit_stops_between_same_line_neighbors(self):
        original = [
            PositionedWord("左侧文字", 10, 10, 40, 16),
            PositionedWord("标题", 55, 10, 30, 16),
            PositionedWord("右侧文字", 90, 10, 40, 16),
        ]
        edited = [
            original[0],
            PositionedWord("这是一个很长的居中标题", 55, 10, 30, 16),
            original[2],
        ]

        fitted = main._center_coordinate_heading_edits(
            original,
            edited,
            ["标题"],
            ["title"],
            source_width=200,
        )

        self.assertGreaterEqual(
            fitted[1].left,
            original[0].left + original[0].width + 2,
        )
        self.assertLessEqual(
            fitted[1].left + fitted[1].width + 1,
            original[2].left - 1,
        )
        self.assertEqual(
            fitted[1].left + fitted[1].width / 2,
            original[1].left + original[1].width / 2,
        )

    def test_centered_heading_edit_observes_page_and_table_boundaries(self):
        original = [PositionedWord("标题", 10, 10, 20, 16)]
        edited = [PositionedWord("这是一个需要更多空间的标题", 10, 10, 20, 16)]
        cells = [[
            TableCell(0, 1, 0, 1, "", 0, 0, 50, 40),
            TableCell(0, 1, 1, 2, "", 50, 0, 100, 40),
        ]]

        fitted = main._center_coordinate_heading_edits(
            original,
            edited,
            ["标题"],
            ["title"],
            cells,
            source_width=80,
        )

        self.assertGreaterEqual(fitted[0].left, 1)
        self.assertLessEqual(fitted[0].left + fitted[0].width + 1, 50)

    def test_centered_heading_edit_stops_before_next_row(self):
        original = [
            PositionedWord("标题", 20, 10, 30, 10),
            PositionedWord("下一行", 20, 40, 30, 10),
        ]
        edited = [
            PositionedWord("一个需要换行的长标题", 20, 10, 30, 60),
            original[1],
        ]

        fitted = main._center_coordinate_heading_edits(
            original,
            edited,
            ["标题"],
            ["title"],
            source_width=200,
            source_height=200,
        )

        self.assertEqual(fitted[0].height, 28)
        self.assertLessEqual(fitted[0].top + fitted[0].height + 1, original[1].top - 1)

    def test_two_centered_edits_do_not_expand_into_each_other(self):
        original = [
            PositionedWord("标题甲", 20, 10, 20, 16),
            PositionedWord("标题乙", 50, 10, 20, 16),
        ]
        edited = [
            PositionedWord("这是很长的标题甲", 20, 10, 20, 16),
            PositionedWord("这是很长的标题乙", 50, 10, 20, 16),
        ]

        fitted = main._center_coordinate_heading_edits(
            original,
            edited,
            ["标题甲", "标题乙"],
            ["title", "title"],
            source_width=200,
        )

        self.assertLessEqual(fitted[0].left + fitted[0].width + 1, fitted[1].left - 1)

    def test_local_ocr_builds_editable_positioned_paragraphs(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            Image.new("RGB", (320, 240), "white").save(image_path)

            with patch(
                "app.services.ocr._tesseract_positioned_words",
                return_value=[("本机识别标题", {"left": 20, "top": 30, "width": 120, "height": 20})],
            ):
                result = recognize_locally(image_path)

        self.assertEqual(result.image_width, 320)
        self.assertEqual(result.image_height, 240)
        self.assertEqual(result.paragraphs, ["本机识别标题"])
        self.assertEqual(result.paragraph_styles, ["body"])
        self.assertEqual(result.positioned_words[0].text, "本机识别标题")

    def test_local_ocr_retries_sparse_page_mode_when_default_mode_misses_text(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            Image.new("RGB", (320, 240), "white").save(image_path)
            local_word = ("表格中的文字", {"left": 20, "top": 30, "width": 120, "height": 20})

            with (
                patch(
                    "app.services.ocr._tesseract_positioned_words",
                    side_effect=[[], [local_word]],
                ) as tesseract,
                patch("app.services.ocr._table_from_image_lines", return_value=None),
            ):
                result = recognize_locally(image_path)

        self.assertEqual(result.paragraphs, ["表格中的文字"])
        self.assertEqual(result.positioned_words[0].text, "表格中的文字")
        self.assertEqual(tesseract.call_count, 2)
        self.assertEqual(tesseract.call_args_list[1].kwargs["page_segmentation_mode"], 11)

    def run_conversion(
        self,
        storage: FakeStorage,
        metadata_root: Path,
        provider,
        *,
        fail_publish=None,
        filename="scan.png",
        media_type="image/png",
        upload_data: bytes | None = None,
        request_headers: dict[str, str] | None = None,
        pdf_failure: str | None = None,
    ):
        metadata_root.mkdir(parents=True, exist_ok=True)
        if fail_publish:
            storage.fail_on = fail_publish

        def write_docx(_result, destination, *_args, **_kwargs):
            Path(destination).write_bytes(b"docx")

        pdf_patch = (
            patch.object(
                main,
                "convert_docx_to_pdf",
                side_effect=RuntimeError(pdf_failure),
            )
            if pdf_failure is not None
            else patch.object(main, "convert_docx_to_pdf", return_value=(None, False))
        )

        with (
            patch.object(main, "storage", storage),
            patch.object(main, "metadata_path", lambda document_id: metadata_root / f"{document_id}.json"),
            patch.object(main, "get_ocr_provider", return_value=provider),
            patch.object(main, "create_docx", side_effect=write_docx),
            pdf_patch,
            TestClient(
                main.app,
                raise_server_exceptions=False,
                client=(f"conversion-test-{uuid4().hex}", 50000),
            ) as client,
        ):
            return client.post(
                "/api/v1/documents/convert",
                files={
                    "image": (
                        filename,
                        png_image() if upload_data is None else upload_data,
                        media_type,
                    )
                },
                headers=request_headers,
            )

    def test_table_conversion_exposes_scan_and_searchable_pdf_variants(self):
        class Provider:
            async def recognize(self, _path):
                cell = TableCell(0, 1, 0, 1, "表格文字", 0, 0, 32, 24)
                return OcrResult(
                    image_width=32,
                    image_height=24,
                    tables=[[['表格文字']]],
                    table_layouts=[TableLayout(
                        column_widths=[32],
                        row_heights=[24],
                        source_width=32,
                        source_left=0,
                        source_right=32,
                    )],
                    table_cells=[[cell]],
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            pdf_calls = []

            def write_docx(_result, output_path, *_args, **_kwargs):
                Path(output_path).write_bytes(b"docx")

            def convert_pdf(docx_path, output_dir, **kwargs):
                pdf_calls.append((Path(docx_path).name, kwargs))
                output = Path(output_dir) / f"{Path(docx_path).stem}.pdf"
                output.write_bytes(b"%PDF-1.7 scan export")
                return output, bool(kwargs.get("prefer_image"))

            metadata_root.mkdir()
            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda document_id: metadata_root / f"{document_id}.json"),
                patch.object(main, "get_ocr_provider", return_value=Provider()),
                patch.object(main, "create_docx", side_effect=write_docx),
                patch.object(main, "create_native_table_docx", side_effect=write_docx),
                patch.object(main, "convert_docx_to_pdf", side_effect=convert_pdf),
                TestClient(main.app, raise_server_exceptions=False) as client,
            ):
                response = client.post(
                    "/api/v1/documents/convert",
                    files={"image": ("scan.png", png_image(), "image/png")},
                )

            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            document_id = body["id"]
            self.assertIn("/download/pdf?", body["pdf_url"])
            self.assertIn("/download/structured-pdf?", body["structured_pdf_url"])
            self.assertEqual(
                [name for name, _kwargs in pdf_calls],
                [f"{document_id}.docx", f"{document_id}_structured.docx"],
            )
            self.assertTrue(pdf_calls[0][1]["prefer_image"])
            self.assertNotIn("fallback_image_path", pdf_calls[1][1])
            self.assertIn(f"{document_id}.pdf", storage.objects)
            self.assertIn(f"{document_id}_structured.pdf", storage.objects)
            self.assertEqual(
                storage.objects[f"{document_id}.pdf"],
                b"%PDF-1.7 scan export",
            )
            metadata = json.loads((metadata_root / f"{document_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["visual_pdf_layout_version"], main.VISUAL_PDF_LAYOUT_VERSION)
            self.assertEqual(metadata["structured_pdf_layout_version"], main.TABLE_PDF_LAYOUT_VERSION)

    def test_ocr_failure_removes_uploaded_and_partial_local_files(self):
        class BrokenProvider:
            async def recognize(self, _path):
                raise RuntimeError("simulated OCR error")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)

            with patch.object(main.logger, "error") as logged_error:
                response = self.run_conversion(
                    storage,
                    metadata_root,
                    BrokenProvider(),
                    request_headers={"Origin": "https://iscan.example"},
                )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(logged_error.call_count, 1)
            log_arguments = logged_error.call_args.args
            self.assertEqual(
                log_arguments[0],
                "Image conversion failed during %s (document_id=%s, error_type=%s)",
            )
            self.assertEqual(log_arguments[1], "ocr")
            self.assertEqual(log_arguments[3], "RuntimeError")
            self.assertNotIn("simulated OCR error", str(log_arguments))
            self.assertEqual(response.headers.get("x-conversion-id"), log_arguments[2])
            self.assertIn(
                "x-conversion-id",
                response.headers.get("access-control-expose-headers", "").lower(),
            )
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(list(metadata_root.iterdir()), [])
            self.assertEqual(storage.published, set())
            self.assertEqual(storage.objects, {})

    def test_known_close_failure_returns_a_stable_retry_code(self):
        class BrokenProvider:
            def __init__(self):
                self.calls = 0

            async def recognize(self, _path):
                self.calls += 1
                raise RuntimeError("Cannot read properties of null (reading 'close')")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            provider = BrokenProvider()
            response = self.run_conversion(
                storage,
                metadata_root,
                provider,
                request_headers={"Origin": "https://iscan.example"},
            )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(provider.calls, 2)
            self.assertEqual(response.json()["detail"], "图片识别服务暂时处理失败，请稍后重试")
            self.assertNotIn("Cannot read properties", response.text)
            self.assertEqual(response.headers.get("x-error-code"), "OCR_NULL_CLOSE")
            self.assertRegex(
                response.headers.get("x-conversion-id", ""),
                r"^[a-f\d]{32}$",
            )
            self.assertIn(
                "x-error-code",
                response.headers.get("access-control-expose-headers", "").lower(),
            )
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(list(metadata_root.iterdir()), [])

    def test_http_exception_close_failure_is_retried_and_sanitized(self):
        class BrokenHttpProvider:
            def __init__(self):
                self.calls = 0

            async def recognize(self, _path):
                self.calls += 1
                raise main.HTTPException(
                    status_code=503,
                    detail="Cannot read properties of null (reading 'close')",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            provider = BrokenHttpProvider()

            response = self.run_conversion(
                storage,
                metadata_root,
                provider,
                request_headers={"Origin": "https://iscan.example"},
            )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(provider.calls, 2)
            self.assertEqual(response.json()["detail"], "图片识别服务暂时处理失败，请稍后重试")
            self.assertNotIn("Cannot read properties", response.text)
            self.assertEqual(response.headers.get("x-error-code"), "OCR_NULL_CLOSE")
            self.assertRegex(response.headers.get("x-conversion-id", ""), r"^[a-f\d]{32}$")
            self.assertIn(
                "x-error-code",
                response.headers.get("access-control-expose-headers", "").lower(),
            )
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(list(metadata_root.iterdir()), [])
            self.assertEqual(storage.published, set())
            self.assertEqual(storage.objects, {})

    def test_known_close_failure_is_retried_once_and_import_succeeds(self):
        class FlakyProvider:
            def __init__(self):
                self.calls = 0

            async def recognize(self, _path):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("Cannot read properties of null (reading 'close')")
                return OcrResult(paragraphs=["识别成功"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            provider = FlakyProvider()

            response = self.run_conversion(storage, metadata_root, provider)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(provider.calls, 2)
            self.assertEqual(response.json()["paragraphs"], ["识别成功"])

    def test_persistent_close_failure_falls_back_to_local_editable_ocr(self):
        class BrokenProvider:
            def __init__(self):
                self.calls = 0

            async def recognize(self, _path):
                self.calls += 1
                raise RuntimeError("Cannot read properties of null (reading 'close')")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            provider = BrokenProvider()
            fallback_result = OcrResult(
                paragraphs=["本机 OCR 识别结果"],
                paragraph_styles=["body"],
                positioned_words=[PositionedWord("本机 OCR 识别结果", 2, 2, 20, 5)],
            )

            with patch.object(main, "recognize_locally", return_value=fallback_result):
                response = self.run_conversion(storage, metadata_root, provider)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(provider.calls, 2)
            self.assertEqual(response.json()["paragraphs"], ["本机 OCR 识别结果"])
            self.assertIn("已使用本机 OCR", "；".join(response.json()["warnings"]))
            self.assertIn("contenteditable", response.json()["editor_html"])
            saved_metadata = json.loads(
                next(metadata_root.glob("*.json")).read_text(encoding="utf-8")
            )
            self.assertIn("已使用本机 OCR", "；".join(saved_metadata["warnings"]))

    def test_transient_upstream_5xx_falls_back_to_local_editable_ocr(self):
        class BrokenProvider:
            def __init__(self):
                self.calls = 0

            async def recognize(self, _path):
                self.calls += 1
                request = httpx.Request("POST", "https://ocr.example/recognize")
                response = httpx.Response(503, request=request, text="upstream unavailable")
                raise httpx.HTTPStatusError("upstream failure", request=request, response=response)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            provider = BrokenProvider()
            fallback_result = OcrResult(
                paragraphs=["本机 OCR 识别结果"],
                paragraph_styles=["body"],
                positioned_words=[PositionedWord("本机 OCR 识别结果", 2, 2, 20, 5)],
            )

            with patch.object(main, "recognize_locally", return_value=fallback_result):
                response = self.run_conversion(storage, metadata_root, provider)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(response.json()["paragraphs"], ["本机 OCR 识别结果"])
        self.assertIn("已使用本机 OCR", "；".join(response.json()["warnings"]))

    def test_transient_upstream_error_returns_retry_signal_when_local_ocr_finds_no_text(self):
        class BrokenProvider:
            def __init__(self):
                self.calls = 0

            async def recognize(self, _path):
                self.calls += 1
                request = httpx.Request(
                    "POST",
                    "https://ocr.example/recognize?access_token=do-not-leak",
                )
                response = httpx.Response(
                    503,
                    request=request,
                    text="upstream unavailable",
                    headers={"Retry-After": "4"},
                )
                raise httpx.HTTPStatusError(
                    "upstream failure",
                    request=request,
                    response=response,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            provider = BrokenProvider()

            with patch.object(main, "recognize_locally", return_value=OcrResult()):
                response = self.run_conversion(storage, metadata_root, provider)
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(list(metadata_root.iterdir()), [])

        self.assertEqual(response.status_code, 503)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(response.headers.get("x-error-code"), "OCR_TRANSIENT")
        self.assertEqual(response.headers.get("retry-after"), "4")
        self.assertNotIn("do-not-leak", response.text)

    def test_close_error_in_upstream_http_body_uses_local_ocr(self):
        class BrokenProvider:
            def __init__(self):
                self.calls = 0

            async def recognize(self, _path):
                self.calls += 1
                request = httpx.Request("POST", "https://ocr.example/recognize")
                response = httpx.Response(
                    400,
                    request=request,
                    text="Cannot read properties of null (reading 'close')",
                )
                raise httpx.HTTPStatusError("upstream failure", request=request, response=response)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            provider = BrokenProvider()
            fallback_result = OcrResult(paragraphs=["本机 OCR 识别结果"])

            with patch.object(main, "recognize_locally", return_value=fallback_result):
                response = self.run_conversion(storage, metadata_root, provider)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(response.json()["paragraphs"], ["本机 OCR 识别结果"])

    def test_http_error_response_never_exposes_upstream_access_token(self):
        class UnauthorizedProvider:
            async def recognize(self, _path):
                request = httpx.Request(
                    "POST",
                    "https://ocr.example/recognize?access_token=do-not-leak",
                )
                response = httpx.Response(401, request=request, text="unauthorized")
                raise httpx.HTTPStatusError("upstream failure", request=request, response=response)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            response = self.run_conversion(
                storage,
                metadata_root,
                UnauthorizedProvider(),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "自建 OCR 服务暂时不可用，请稍后重试")
        self.assertNotIn("do-not-leak", response.text)

    def test_pdf_conversion_warning_does_not_expose_local_details(self):
        class Provider:
            async def recognize(self, _path):
                return OcrResult(paragraphs=["测试正文"], paragraph_styles=["body"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            response = self.run_conversion(
                FakeStorage(root),
                metadata_root,
                Provider(),
                pdf_failure="secret local path /srv/private/convert.log",
            )

        self.assertEqual(response.status_code, 200, response.text)
        warnings = " ".join(response.json()["warnings"])
        self.assertIn("PDF 转换失败，请稍后重试", warnings)
        self.assertNotIn("/srv/private", warnings)
        self.assertNotIn("convert.log", warnings)

    def test_partial_remote_publication_is_rolled_back_and_local_files_removed(self):
        class Provider:
            async def recognize(self, _path):
                return OcrResult(paragraphs=["测试正文"], paragraph_styles=["body"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root, fail_on="_source.png")

            response = self.run_conversion(storage, metadata_root, Provider())

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"], "识别结果暂时无法保存，请稍后重试")
            self.assertNotIn("simulated object upload error", response.text)
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(list(metadata_root.iterdir()), [])
            self.assertEqual(storage.published, set())
            self.assertEqual(storage.objects, {})

    def test_accepts_and_preserves_source_bytes_above_ocr_upload_limit(self):
        class Provider:
            async def recognize(self, _path):
                return OcrResult(paragraphs=["测试正文"], paragraph_styles=["body"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            original_png = png_image() + (b"\0" * (1024 * 1024))

            with (
                patch.object(main, "max_upload_bytes", return_value=512 * 1024),
                patch.object(main, "max_source_image_bytes", return_value=2 * 1024 * 1024),
            ):
                response = self.run_conversion(
                    storage,
                    metadata_root,
                    Provider(),
                    upload_data=original_png,
                )

            self.assertEqual(response.status_code, 200, response.text)
            source_name = next(name for name in storage.objects if name.endswith("_source.png"))
            self.assertEqual(storage.objects[source_name], original_png)

    def test_preserves_original_png_and_scales_ocr_geometry_to_it(self):
        class Provider:
            async def recognize(self, _path):
                return OcrResult(
                    paragraphs=["测试正文"],
                    paragraph_styles=["body"],
                    positioned_words=[PositionedWord("测试", 1000, 100, 100, 20)],
                    paragraph_layouts=[ParagraphLayout(
                        fragments=[TextFragment("测试", 1000, 100, 500, 25)],
                        source_width=2500,
                        left=1000,
                        top=100,
                        right=1500,
                        bottom=125,
                    )],
                    tables=[[['单元格']]],
                    table_layouts=[TableLayout(
                        column_widths=[1250, 1250],
                        row_heights=[125],
                        source_width=2500,
                        source_left=100,
                        source_right=1900,
                    )],
                    table_cells=[[TableCell(0, 1, 0, 1, "单元格", 100, 200, 1000, 400)]],
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            original_png = png_image(3000, 1500)
            source_dimensions = {}

            def write_docx(_result, destination, source_path, *_args, **_kwargs):
                with Image.open(source_path) as source:
                    source_dimensions["size"] = source.size
                    source_dimensions["format"] = source.format
                Path(destination).write_bytes(b"docx")

            metadata_root.mkdir()
            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda document_id: metadata_root / f"{document_id}.json"),
                patch.object(main, "get_ocr_provider", return_value=Provider()),
                patch.object(main, "create_docx", side_effect=write_docx),
                patch.object(main, "create_hybrid_editable_docx", side_effect=write_docx),
                patch.object(main, "convert_docx_to_pdf", return_value=(None, False)),
                patch.object(main, "asdict", wraps=main.asdict) as serialized_dataclass,
                TestClient(main.app, raise_server_exceptions=False) as client,
            ):
                response = client.post(
                    "/api/v1/documents/convert",
                    files={"image": ("scan.png", original_png, "image/png")},
                )

            serialized_types = [type(call.args[0]) for call in serialized_dataclass.call_args_list]
            self.assertEqual(serialized_types.count(PositionedWord), 1)
            self.assertEqual(serialized_types.count(ParagraphLayout), 1)
            self.assertEqual(serialized_types.count(TableLayout), 1)
            self.assertEqual(serialized_types.count(TableCell), 1)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["image_width"], 3000)
            self.assertEqual(body["image_height"], 1500)
            self.assertEqual(body["positioned_words"][0]["left"], 1200)
            self.assertEqual(body["positioned_words"][0]["top"], 120)
            self.assertEqual(body["positioned_words"][0]["width"], 120)
            self.assertEqual(body["positioned_words"][0]["height"], 24)
            metadata = json.loads(next(metadata_root.glob("*.json")).read_text(encoding="utf-8"))
            self.assertTrue(metadata["positioned_table_words"])
            self.assertEqual(metadata["paragraph_layouts"][0]["fragments"][0]["left"], 1200)
            self.assertEqual(body["table_layouts"][0]["source_width"], 3000)
            self.assertEqual(body["table_layouts"][0]["column_widths"], [1500, 1500])
            self.assertEqual(body["table_layouts"][0]["row_heights"], [150])
            self.assertEqual(body["table_cells"][0][0]["left"], 120)
            self.assertEqual(body["table_cells"][0][0]["bottom"], 480)
            self.assertNotIn("source_positioned_words", metadata)
            self.assertNotIn("source_table_cells", metadata)
            self.assertEqual(source_dimensions, {"size": (3000, 1500), "format": "PNG"})
            source_names = [name for name in storage.objects if name.endswith("_source.png")]
            self.assertEqual(len(source_names), 1)
            self.assertEqual(storage.objects[source_names[0]], original_png)

    def test_preserves_full_resolution_tiff_source_as_lossless_png(self):
        class Provider:
            async def recognize(self, _path):
                return OcrResult(paragraphs=["测试正文"], paragraph_styles=["body"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root)
            source = Image.new("RGB", (2400, 1200), (18, 45, 90))
            source.putpixel((1234, 567), (240, 210, 30))
            expected_pixels = source.tobytes()
            source_tiff = io.BytesIO()
            source.save(source_tiff, format="TIFF")
            source.close()

            response = self.run_conversion(
                storage,
                metadata_root,
                Provider(),
                filename="scan.tiff",
                media_type="image/tiff",
                upload_data=source_tiff.getvalue(),
            )

            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual((body["image_width"], body["image_height"]), (2400, 1200))
            source_names = [name for name in storage.objects if name.endswith("_source.png")]
            self.assertEqual(len(source_names), 1)
            with Image.open(io.BytesIO(storage.objects[source_names[0]])) as preserved:
                self.assertEqual(preserved.format, "PNG")
                self.assertEqual(preserved.size, (2400, 1200))
                self.assertEqual(preserved.tobytes(), expected_pixels)

    def test_partial_edited_document_publication_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            storage = FakeStorage(root, fail_on=".docx")
            metadata_root.mkdir()

            def write_docx(_result, destination, *_args, **_kwargs):
                Path(destination).write_bytes(b"docx")

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda document_id: metadata_root / f"{document_id}.json"),
                patch.object(main, "create_docx", side_effect=write_docx),
                patch.object(main, "convert_docx_to_pdf", return_value=(None, False)),
                TestClient(main.app, raise_server_exceptions=False) as client,
            ):
                response = client.post(
                    "/api/v1/documents/export-edited",
                    json={"paragraphs": ["编辑后的文字"]},
                )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"], "编辑后的文件暂时无法保存，请稍后重试")
            self.assertNotIn("simulated object upload error", response.text)
            self.assertEqual(storage.published, set())
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(list(metadata_root.iterdir()), [])
            self.assertEqual(storage.objects, {})

    def test_edited_pdf_conversion_warning_does_not_expose_local_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "storage"
            metadata_root = Path(directory) / "metadata"
            root.mkdir()
            metadata_root.mkdir()
            storage = FakeStorage(root)

            def write_docx(_result, destination, *_args, **_kwargs):
                Path(destination).write_bytes(b"docx")

            with (
                patch.object(main, "storage", storage),
                patch.object(main, "metadata_path", lambda document_id: metadata_root / f"{document_id}.json"),
                patch.object(main, "create_docx", side_effect=write_docx),
                patch.object(
                    main,
                    "convert_docx_to_pdf",
                    side_effect=RuntimeError("secret local path /srv/private/convert.log"),
                ),
                TestClient(main.app, raise_server_exceptions=False) as client,
            ):
                response = client.post(
                    "/api/v1/documents/export-edited",
                    json={"paragraphs": ["编辑后的文字"]},
                )

        self.assertEqual(response.status_code, 200, response.text)
        warnings = " ".join(response.json()["warnings"])
        self.assertIn("PDF 转换失败，请稍后重试", warnings)
        self.assertNotIn("/srv/private", warnings)
        self.assertNotIn("convert.log", warnings)


if __name__ == "__main__":
    unittest.main()
