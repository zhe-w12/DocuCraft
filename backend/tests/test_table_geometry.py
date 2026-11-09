import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from docx import Document
from docx.oxml.ns import qn


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.ocr import (
    OcrResult,
    PositionedWord,
    TableCell,
    TableLayout,
    _is_low_confidence_table_rule_noise,
    _generic_positioned_words,
    _generic_table_cells,
    _generic_table_layouts,
    _positioned_paragraph_candidates,
    _refine_table_merges_from_image_lines,
    _recognize_dense_merged_table_cell,
    _recognize_vertical_section_label,
    _restore_vertical_section_label,
    _table_from_image_lines,
)
from app.services.documents import create_native_table_docx
from app.services.table_geometry import _detect_table_line_guides_pillow


class PillowTableGeometryTests(unittest.TestCase):
    def test_fallback_detects_horizontal_and_vertical_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.jpg"
            image = Image.new("L", (220, 220), 255)
            draw = ImageDraw.Draw(image)
            for x in (10, 90, 180):
                draw.line((x, 10, x, 210), fill=40, width=2)
            for y in (10, 70, 140, 210):
                draw.line((10, y, 180, y), fill=40, width=2)
            image.save(path, quality=95)

            guides = _detect_table_line_guides_pillow(path, (0, 0, 220, 220))

            self.assertEqual(len(guides.x), 3)
            self.assertEqual(len(guides.y), 4)
            self.assertTrue(all(abs(actual - expected) <= 2 for actual, expected in zip(guides.x, (10, 90, 180))))
            self.assertTrue(all(abs(actual - expected) <= 2 for actual, expected in zip(guides.y, (10, 70, 140, 210))))

    def test_image_lines_rebuild_dynamic_table_and_title(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.jpg"
            image = Image.new("L", (300, 300), 255)
            draw = ImageDraw.Draw(image)
            for x in (20, 120, 280):
                draw.line((x, 80, x, 280), fill=40, width=2)
            for y in (80, 150, 280):
                draw.line((20, y, 280, y), fill=40, width=2)
            image.save(path, quality=95)
            words = [
                ("表格标题", {"left": 100, "top": 20, "width": 100, "height": 20}),
                ("编号", {"left": 35, "top": 95, "width": 45, "height": 18}),
                ("名称", {"left": 145, "top": 95, "width": 45, "height": 18}),
                ("1", {"left": 50, "top": 180, "width": 10, "height": 18}),
                ("示例", {"left": 150, "top": 180, "width": 35, "height": 18}),
            ]

            result = _table_from_image_lines(path, words)

            self.assertIsNotNone(result)
            self.assertEqual(result.paragraphs, ["表格标题"])
            self.assertEqual(result.paragraph_styles, ["title"])
            self.assertEqual(result.tables, [[["编号", "名称"], ["1", "示例"]]])
            self.assertAlmostEqual(result.table_layouts[0].source_left, 20, delta=2)
            self.assertAlmostEqual(result.table_layouts[0].source_right, 280, delta=2)

    def test_image_lines_rebuild_partial_rules_as_native_merged_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "merged-form.jpg"
            image = Image.new("L", (240, 180), 255)
            draw = ImageDraw.Draw(image)
            draw.line((20, 8, 220, 8), fill=100, width=2)
            for x in (20, 220):
                draw.line((x, 20, x, 160), fill=30, width=2)
            draw.line((60, 60, 60, 160), fill=30, width=2)
            for y in (20, 60, 160):
                draw.line((20, y, 220, y), fill=30, width=2)
            draw.line((60, 110, 220, 110), fill=30, width=2)
            image.save(path, quality=95)
            image.close()

            words = [
                ("项目", {"left": 45, "top": 30, "width": 40, "height": 18}),
                ("家", {"left": 30, "top": 65, "width": 15, "height": 14}),
                ("庭", {"left": 30, "top": 85, "width": 15, "height": 14}),
                ("本", {"left": 30, "top": 115, "width": 15, "height": 14}),
                ("况", {"left": 30, "top": 140, "width": 15, "height": 14}),
                ("甲", {"left": 120, "top": 75, "width": 18, "height": 18}),
                ("乙", {"left": 120, "top": 125, "width": 18, "height": 18}),
            ]

            result = _table_from_image_lines(path, words)

            self.assertIsNotNone(result)
            self.assertEqual(
                result.tables,
                [[
                    ["项目", ""],
                    ["家\n庭\n基\n本\n情\n况", "甲"],
                    ["", "乙"],
                ]],
            )
            spans = {
                (cell.row_start, cell.row_end, cell.col_start, cell.col_end)
                for cell in result.table_cells[0]
            }
            self.assertIn((0, 1, 0, 2), spans)
            self.assertIn((1, 3, 0, 1), spans)
            self.assertAlmostEqual(result.table_layouts[0].row_heights[0], 40, delta=3)

            exported = Path(directory) / "merged-form.docx"
            create_native_table_docx(result, exported)
            table = Document(exported).tables[0]
            self.assertIs(table.cell(0, 0)._tc, table.cell(0, 1)._tc)
            self.assertIs(table.cell(1, 0)._tc, table.cell(2, 0)._tc)
            vertical_label = table.cell(1, 0)
            self.assertEqual(vertical_label.text, "家\n庭\n基\n本\n情\n况")
            self.assertIsNone(vertical_label._tc.tcPr.find(qn("w:textDirection")))
            self.assertTrue(
                all(
                    run.bold is False
                    for paragraph in vertical_label.paragraphs
                    for run in paragraph.runs
                )
            )

    def test_image_lines_recovers_a_missed_vertical_label_from_its_own_cell(self):
        expected_label = "家庭基本情况"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vertical-label.jpg"
            image = Image.new("L", (240, 180), 255)
            draw = ImageDraw.Draw(image)
            draw.line((20, 8, 220, 8), fill=100, width=2)
            for x in (20, 220):
                draw.line((x, 20, x, 160), fill=30, width=2)
            draw.line((60, 60, 60, 160), fill=30, width=2)
            for y in (20, 60, 160):
                draw.line((20, y, 220, y), fill=30, width=2)
            draw.line((60, 110, 220, 110), fill=30, width=2)
            image.save(path, quality=95)
            image.close()
            words = [
                ("项目", {"left": 45, "top": 30, "width": 40, "height": 18}),
                ("甲", {"left": 120, "top": 75, "width": 18, "height": 18}),
                ("乙", {"left": 120, "top": 125, "width": 18, "height": 18}),
            ]
            crop_sizes = []

            def recognize_cropped_label(
                crop_path,
                *,
                page_segmentation_mode,
                timeout_seconds,
                word_confidences=None,
                **_kwargs,
            ):
                with Image.open(crop_path) as crop:
                    crop_sizes.append(crop.size)
                self.assertEqual(page_segmentation_mode, 6)
                self.assertEqual(timeout_seconds, 5)
                if word_confidences is not None:
                    word_confidences.append((93.0, expected_label))
                return [(
                    expected_label,
                    {"left": 2, "top": 2, "width": 36, "height": 96},
                )]

            with patch(
                "app.services.ocr._tesseract_positioned_words",
                side_effect=recognize_cropped_label,
            ) as local_ocr:
                result = _table_from_image_lines(path, words)

        self.assertIsNotNone(result)
        self.assertEqual(local_ocr.call_count, 1)
        self.assertEqual(len(crop_sizes), 1)
        self.assertGreater(crop_sizes[0][1], crop_sizes[0][0] * 2)
        self.assertEqual(
            result.tables[0][1][0],
            "家\n庭\n基\n本\n情\n况",
        )

    def test_vertical_label_crop_rejects_low_confidence_recognition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "faint-label.png"
            Image.new("RGB", (60, 180), "white").save(path)

            def recognize_faint_label(
                _crop_path,
                *,
                word_confidences=None,
                **_kwargs,
            ):
                if word_confidences is not None:
                    word_confidences.append((55.0, "家庭基本情况"))
                return [("家庭基本情况", {"left": 0, "top": 0, "width": 50, "height": 160})]

            with patch(
                "app.services.ocr._tesseract_positioned_words",
                side_effect=recognize_faint_label,
            ):
                result = _recognize_vertical_section_label(path, 0, 0, 60, 180)

        self.assertIsNone(result)

    def test_image_lines_re_reads_dense_merged_cell_in_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dense-merged-cell.png"
            image = Image.new("L", (400, 420), 255)
            draw = ImageDraw.Draw(image)
            for x in (20, 150, 380):
                draw.line((x, 20, x, 370), fill=30, width=2)
            draw.line((20, 20, 380, 20), fill=30, width=2)
            draw.line((20, 70, 380, 70), fill=30, width=2)
            for y in (120, 170, 220, 270, 320):
                draw.line((20, y, 150, y), fill=30, width=2)
            draw.line((20, 370, 380, 370), fill=30, width=2)
            image.save(path)
            image.close()

            source_line = "□父母及家庭经济状况核验说明内容"
            words = [
                (
                    source_line,
                    {"left": 170, "top": top, "width": 190, "height": 22},
                )
                for top in (82, 132, 182, 232, 282, 332)
            ]
            recognized_lines = [
                "□父母工作情况说明和家庭收入资料清单",
                "□父母年龄及赡养老人情况核验材料",
                "□农村种植户或养殖户经营情况说明",
                "□在学人数和家庭成员基本情况核验",
                "□申请人家庭经济情况及证明材料清单",
                "□其他家庭困难情况补充说明及材料",
            ]
            captured_crop_sizes = []

            def recognize_cropped_cell(
                crop_path,
                *,
                page_segmentation_mode,
                timeout_seconds,
                word_confidences=None,
                **_kwargs,
            ):
                with Image.open(crop_path) as crop:
                    captured_crop_sizes.append(crop.size)
                self.assertEqual(page_segmentation_mode, 6)
                self.assertEqual(timeout_seconds, 5)
                if word_confidences is not None:
                    word_confidences.extend((95.0, line) for line in recognized_lines)
                return [
                    (
                        line,
                        {"left": 5, "top": 8 + index * 40, "width": 250, "height": 20},
                    )
                    for index, line in enumerate(recognized_lines)
                ]

            with patch(
                "app.services.ocr._tesseract_positioned_words",
                side_effect=recognize_cropped_cell,
            ) as local_ocr:
                result = _table_from_image_lines(path, words)

        self.assertIsNotNone(result)
        self.assertEqual(local_ocr.call_count, 1)
        self.assertEqual(len(captured_crop_sizes), 1)
        self.assertGreater(captured_crop_sizes[0][0], 200)
        self.assertGreater(captured_crop_sizes[0][1], 250)
        expected = "\n".join(recognized_lines)
        self.assertEqual(result.tables[0][1][1], expected)
        recognized_cell = next(
            cell
            for cell in result.table_cells[0]
            if (cell.row_start, cell.col_start) == (1, 1)
        )
        self.assertEqual(recognized_cell.text, expected)
        recognized_positions = [
            word for word in result.positioned_words if word.text in recognized_lines
        ]
        self.assertEqual(len(recognized_positions), len(recognized_lines))
        self.assertTrue(all(word.left >= 150 and word.top >= 70 for word in recognized_positions))
        self.assertFalse(any(word.text == source_line for word in result.positioned_words))

    def test_dense_merged_cell_crop_rejects_low_confidence_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dense-cell.png"
            Image.new("RGB", (320, 320), "white").save(path)

            def recognize_low_confidence(
                _crop_path,
                *,
                word_confidences=None,
                **_kwargs,
            ):
                if word_confidences is not None:
                    word_confidences.extend((55.0, "低置信度候选文字") for _ in range(3))
                return [
                    ("低置信度候选文字", {"left": 5, "top": 5, "width": 80, "height": 18})
                    for _ in range(3)
                ]

            with patch(
                "app.services.ocr._tesseract_positioned_words",
                side_effect=recognize_low_confidence,
            ):
                result = _recognize_dense_merged_table_cell(
                    path,
                    "□已有清单" * 20,
                    10,
                    10,
                    300,
                    300,
                )

        self.assertIsNone(result)

    def test_low_confidence_table_rule_noise_is_filtered_but_values_are_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table-noise.png"
            image = Image.new("L", (240, 200), 255)
            draw = ImageDraw.Draw(image)
            for x in (10, 100, 230):
                draw.line((x, 20, x, 180), fill=30, width=2)
            for y in (20, 80, 180):
                draw.line((10, y, 230, y), fill=30, width=2)
            image.save(path)
            image.close()

            result = _table_from_image_lines(
                path,
                [
                    ("编号", {"left": 25, "top": 35, "width": 35, "height": 18}),
                    (
                        "ff]",
                        {
                            "left": 120,
                            "top": 35,
                            "width": 60,
                            "height": 18,
                            "confidence": 56,
                        },
                    ),
                    (
                        "12",
                        {
                            "left": 120,
                            "top": 50,
                            "width": 22,
                            "height": 18,
                            "confidence": 40,
                        },
                    ),
                    (
                        "Li",
                        {
                            "left": 150,
                            "top": 50,
                            "width": 22,
                            "height": 18,
                            "confidence": 20,
                        },
                    ),
                    ("A]", {"left": 180, "top": 50, "width": 22, "height": 18}),
                    (
                        "姓名",
                        {
                            "left": 25,
                            "top": 100,
                            "width": 35,
                            "height": 18,
                            "confidence": 40,
                        },
                    ),
                ],
            )

        self.assertIsNotNone(result)
        self.assertNotIn("ff]", result.tables[0][0][1])
        self.assertIn("12", result.tables[0][0][1])
        self.assertIn("Li", result.tables[0][0][1])
        self.assertIn("A]", result.tables[0][0][1])
        self.assertEqual(result.tables[0][1][0], "姓名")
        self.assertFalse(any(word.text == "ff]" for word in result.positioned_words))
        self.assertFalse(_is_low_confidence_table_rule_noise("Li", {"confidence": 20}))
        self.assertFalse(_is_low_confidence_table_rule_noise("A]", {}))
        self.assertTrue(
            _is_low_confidence_table_rule_noise(
                "r",
                {"confidence": 10, "width": 43, "height": 64},
            )
        )

    def test_image_rules_refine_cloud_table_merges_when_text_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloud-table.jpg"
            image = Image.new("L", (240, 180), 255)
            draw = ImageDraw.Draw(image)
            draw.line((20, 8, 220, 8), fill=100, width=2)
            for x in (20, 220):
                draw.line((x, 20, x, 160), fill=30, width=2)
            draw.line((60, 60, 60, 160), fill=30, width=2)
            for y in (20, 60, 160):
                draw.line((20, y, 220, y), fill=30, width=2)
            draw.line((60, 110, 220, 110), fill=30, width=2)
            image.save(path, quality=95)
            image.close()

            words = [
                PositionedWord("项目", 45, 30, 40, 18),
                PositionedWord("家", 30, 65, 15, 14),
                PositionedWord("庭", 30, 80, 15, 14),
                PositionedWord("基", 30, 95, 15, 14),
                PositionedWord("本", 30, 110, 15, 14),
                PositionedWord("情", 30, 125, 15, 14),
                PositionedWord("况", 30, 140, 15, 14),
                PositionedWord("甲", 120, 75, 18, 18),
                PositionedWord("乙", 120, 125, 18, 18),
            ]
            result = OcrResult(
                image_width=240,
                image_height=180,
                tables=[
                    [
                        ["项目", ""],
                        ["家\n庭\n基\n本\n情\n况", "甲"],
                        ["", "乙"],
                    ]
                ],
                table_layouts=[
                    TableLayout(
                        column_widths=[40, 160],
                        row_heights=[40, 50, 50],
                        source_width=240,
                        source_left=20,
                        source_right=220,
                    )
                ],
                table_cells=[
                    [
                        TableCell(row, row + 1, column, column + 1, text, left, top, right, bottom)
                        for row, row_values, top, bottom in (
                            (0, ["项目", ""], 20, 60),
                            (1, ["家庭基本情况", "甲"], 60, 110),
                            (2, ["", "乙"], 110, 160),
                        )
                        for column, (text, left, right) in enumerate(
                            ((row_values[0], 20, 60), (row_values[1], 60, 220))
                        )
                    ]
                ],
                positioned_words=words,
            )

            changed = _refine_table_merges_from_image_lines(result, path)

            self.assertTrue(changed)
            self.assertEqual(
                result.tables,
                [[
                    ["项目", ""],
                    ["家\n庭\n基\n本\n情\n况", "甲"],
                    ["", "乙"],
                ]],
            )
            spans = {
                (cell.row_start, cell.row_end, cell.col_start, cell.col_end)
                for cell in result.table_cells[0]
            }
            self.assertIn((0, 1, 0, 2), spans)
            self.assertIn((1, 3, 0, 1), spans)

    def test_image_rules_do_not_replace_cloud_text_with_unmatched_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloud-table.jpg"
            image = Image.new("L", (240, 180), 255)
            draw = ImageDraw.Draw(image)
            for x in (20, 60, 220):
                draw.line((x, 20, x, 160), fill=30, width=2)
            for y in (20, 60, 110, 160):
                draw.line((20, y, 220, y), fill=30, width=2)
            image.save(path, quality=95)
            image.close()

            cells = [
                TableCell(0, 1, 0, 1, "云端完整识别文字", 20, 20, 60, 60),
                TableCell(0, 1, 1, 2, "", 60, 20, 220, 60),
                TableCell(1, 2, 0, 1, "", 20, 60, 60, 110),
                TableCell(1, 2, 1, 2, "", 60, 60, 220, 110),
                TableCell(2, 3, 0, 1, "", 20, 110, 60, 160),
                TableCell(2, 3, 1, 2, "", 60, 110, 220, 160),
            ]
            result = OcrResult(
                image_width=240,
                image_height=180,
                tables=[[["云端完整识别文字", ""], ["", ""], ["", ""]]],
                table_layouts=[TableLayout([40, 160], [40, 50, 50], 240, 20, 220)],
                table_cells=[cells],
                positioned_words=[PositionedWord("错位文字", 25, 25, 30, 15)],
            )
            original_grid = [row[:] for row in result.tables[0]]
            original_spans = [
                (cell.row_start, cell.row_end, cell.col_start, cell.col_end)
                for cell in result.table_cells[0]
            ]

            changed = _refine_table_merges_from_image_lines(result, path)

            self.assertFalse(changed)
            self.assertEqual(result.tables[0], original_grid)
            self.assertEqual(
                [
                    (cell.row_start, cell.row_end, cell.col_start, cell.col_end)
                    for cell in result.table_cells[0]
                ],
                original_spans,
            )

    def test_vertical_label_recovery_requires_a_tall_matching_label(self):
        self.assertEqual(
            _restore_vertical_section_label("家\n庭\n本\n况", 10, 20, 40, 100),
            "家\n庭\n基\n本\n情\n况",
        )
        self.assertEqual(
            _restore_vertical_section_label("家庭本况", 10, 20, 40, 70),
            "家庭本况",
        )
        self.assertEqual(
            _restore_vertical_section_label("家人本况", 10, 20, 40, 100),
            "家人本况",
        )

    def test_coordinate_ocr_preserves_visual_heading_levels(self):
        words = [
            ("文档标题", {"left": 180, "top": 12, "width": 240, "height": 30}),
            ("副标题", {"left": 250, "top": 58, "width": 100, "height": 20}),
            ("这是正文内容。", {"left": 24, "top": 112, "width": 420, "height": 14}),
        ]

        candidates = _positioned_paragraph_candidates(words, image_width=600)

        self.assertEqual([item[0] for item in candidates], ["文档标题", "副标题", "这是正文内容。"])
        self.assertEqual([item[1] for item in candidates], ["title", "subtitle", "body"])
        self.assertEqual(candidates[0][3].source_width, 600)

    def test_generic_ocr_normalizes_coordinates_and_table_geometry(self):
        positioned = _generic_positioned_words(
            {
                "words_result": [
                    {
                        "words": "标题",
                        "location": {"left": 100, "top": 12, "width": 80, "height": 24},
                    },
                    {"text": "编号", "bbox": [20, 90, 60, 20]},
                    {
                        "content": "名称",
                        "polygon": [
                            {"x": 120, "y": 90},
                            {"x": 180, "y": 90},
                            {"x": 180, "y": 110},
                            {"x": 120, "y": 110},
                        ],
                    },
                ]
            }
        )

        self.assertEqual(positioned[0][1], {"left": 100, "top": 12, "width": 80, "height": 24})
        self.assertEqual(positioned[1][1], {"left": 20, "top": 90, "width": 60, "height": 20})
        self.assertEqual(positioned[2][1], {"left": 120, "top": 90, "width": 60, "height": 20})

        layouts = _generic_table_layouts(
            [{"column_widths": [100, 200], "row_heights": [30], "source_right": 300}],
            600,
        )
        cells = _generic_table_cells(
            [[{"row_start": 0, "row_end": 1, "col_start": 0, "col_end": 2, "text": "合并"}]]
        )
        self.assertEqual(layouts[0].source_width, 600)
        self.assertEqual(cells[0][0].col_end, 2)


if __name__ == "__main__":
    unittest.main()
