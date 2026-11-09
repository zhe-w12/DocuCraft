import random
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageCms, ImageDraw


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.documents import (
    _centered_paragraph_texts,
    _coordinate_word_index,
    _create_hybrid_editable_background,
    _create_image_pdf,
    _editable_textbox_font_size,
    _erase_positioned_text,
    _first_coordinate_word_match,
    _fit_coordinate_word_edits,
    _has_positioned_word_changes,
    _image_on_white,
    _is_centered_table_word,
    _merge_coordinate_words,
    _native_cell_text,
    _native_merge_plan,
    _native_side_margin,
    _native_word_text,
    _preserve_native_merged_text,
    _positioned_words_by_table_cell,
    _same_positioned_word,
    _set_vertical_section_label,
    _table_cell_coordinate_index,
    _table_cells_by_origin,
    _table_cell_memberships,
    _table_cell_order_index,
    _table_cells_ordered_at_center,
    _table_contains_center,
    _is_centered_table_word,
    create_docx,
    create_hybrid_editable_docx,
    create_native_table_docx,
    render_positioned_image,
)
from app.services.ocr import (
    OcrResult,
    ParagraphLayout,
    PositionedWord,
    TableCell,
    TableLayout,
    TextFragment,
    clean_ocr_text,
)
from app.services.storage import is_non_empty_file


class NativeDocumentLayoutTests(unittest.TestCase):
    def test_native_merged_text_uses_full_source_once_and_keeps_distinct_cell_text(self):
        source = "□父母均没有工作\n□父母一方没有工作\n家庭人均年收入________元"
        existing = (
            "□父母均没有工作\n"
            "□父母一方没有工作\n"
            "家庭人均年收入________元\n"
            "□父母均没有工作\n"
            "额外字段"
        )

        merged = _preserve_native_merged_text(existing, source)

        self.assertEqual(
            merged,
            f"{source}\n额外字段",
        )

    def test_native_merged_text_deduplicates_generated_income_field_underscores(self):
        source = "□家庭情况清单\n家庭人均年收入（人民币元）"
        existing = (
            "□家庭情况清单\n"
            "家庭人均年收入________________（人民币\n"
            "元）"
        )

        merged = _preserve_native_merged_text(existing, source)

        self.assertEqual(merged, source)

    def test_native_merged_text_deduplicates_repeated_checklist_block(self):
        checklist = [
            "□父母均没有工作（不含农村种植户或养殖户）",
            "□父母一方没有工作（不含农村种植户或养殖户）",
            "□农村个体小型种植户或个体小型养殖户（或两者均是）",
        ]
        source = "\n".join([*checklist, "家庭人均年收入________（人民币元）", *checklist])

        merged = _preserve_native_merged_text(source, source)

        self.assertEqual(
            merged,
            "\n".join([*checklist, "家庭人均年收入________（人民币元）"]),
        )

    def test_native_docx_export_deduplicates_repeated_checklist_block(self):
        from docx import Document as WordDocument

        checklist = [
            "□父母均没有工作（不含农村种植户或养殖户）",
            "□父母一方没有工作（不含农村种植户或养殖户）",
            "□农村个体小型种植户或个体小型养殖户（或两者均是）",
        ]
        income = "家庭人均年收入________（人民币元）"
        duplicate_text = "\n".join([*checklist, income, *checklist])
        result = OcrResult(
            tables=[[[duplicate_text], [checklist[0]], [checklist[1]], [income]]],
            table_layouts=[TableLayout(
                column_widths=[200],
                row_heights=[20, 20, 20, 20],
                source_width=200,
                source_left=0,
                source_right=200,
            )],
            table_cells=[[
                TableCell(0, 4, 0, 1, duplicate_text, 0, 0, 200, 80),
            ]],
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "deduplicated-checklist.docx"
            create_native_table_docx(result, output)
            exported_text = WordDocument(output).tables[0].cell(0, 0).text

        self.assertEqual(exported_text, "\n".join([*checklist, income]))

    def test_native_section_label_collapses_an_exact_duplicate(self):
        from docx import Document as WordDocument

        cell = WordDocument().add_table(rows=1, cols=1).cell(0, 0)
        source_cell = TableCell(
            0,
            1,
            0,
            1,
            "家庭基本情况家庭基本情况",
            0,
            0,
            30,
            180,
        )

        _set_vertical_section_label(cell, source_cell, source_cell.text)

        self.assertEqual(cell.text, "\n".join("家庭基本情况"))

    def test_native_table_page_height_matches_source_aspect_when_rows_fit(self):
        from docx import Document as WordDocument

        result = OcrResult(
            image_width=200,
            image_height=240,
            tables=[[['']]],
            table_layouts=[TableLayout(
                column_widths=[180],
                row_heights=[20],
                source_width=200,
                source_left=10,
                source_right=190,
            )],
            table_cells=[[
                TableCell(0, 1, 0, 1, "", 10, 10, 190, 30),
            ]],
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source-aspect.docx"
            create_native_table_docx(result, output)
            page = WordDocument(output).sections[0]

        self.assertAlmostEqual(
            page.page_height.pt,
            page.page_width.pt * result.image_height / result.image_width,
            delta=0.1,
        )

    def test_native_docx_merge_does_not_repeat_canonical_long_cell_text(self):
        import tempfile

        from docx import Document as WordDocument

        source = "□父母均没有工作\n□父母一方没有工作\n家庭人均年收入________元"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "long-merged-cell.docx"
            result = OcrResult(
                tables=[[
                    [source],
                    ["□父母均没有工作"],
                    ["□父母一方没有工作"],
                    ["家庭人均年收入________元"],
                ]],
                table_layouts=[TableLayout(
                    column_widths=[200],
                    row_heights=[20, 20, 20, 20],
                    source_width=200,
                    source_left=0,
                    source_right=200,
                )],
                table_cells=[[
                    TableCell(0, 4, 0, 1, source, 0, 0, 200, 80),
                ]],
            )

            create_native_table_docx(result, output)
            document = WordDocument(output)
            exported_text = document.tables[0].cell(0, 0).text

        self.assertEqual(exported_text, source)

    def test_native_docx_keeps_vertical_section_labels_upright(self):
        import tempfile

        from docx import Document as WordDocument
        from docx.oxml.ns import qn

        source = "\n".join("学生基本情况")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "vertical-section-label.docx"
            result = OcrResult(
                tables=[[[source, ""]]],
                table_layouts=[TableLayout(
                    column_widths=[60, 940],
                    row_heights=[300],
                    source_width=1000,
                    source_left=0,
                    source_right=1000,
                )],
                table_cells=[[
                    TableCell(0, 1, 0, 1, source, 0, 0, 60, 300),
                    TableCell(0, 1, 1, 2, "", 60, 0, 1000, 300),
                ]],
            )

            create_native_table_docx(result, output)
            document = WordDocument(output)
            exported_cell = document.tables[0].cell(0, 0)

        self.assertEqual(exported_cell.text, source)
        self.assertIsNone(exported_cell._tc.tcPr.find(qn("w:textDirection")))
        self.assertTrue(
            all(run.bold is False for paragraph in exported_cell.paragraphs for run in paragraph.runs)
        )

    def test_indexed_coordinate_matches_keep_tolerance_and_original_order(self):
        randomizer = random.Random(814)
        words = [
            PositionedWord(
                str(index),
                randomizer.uniform(-100, 100),
                randomizer.uniform(-100, 100),
                12,
                10,
            )
            for index in range(250)
        ]
        words.extend(
            [
                PositionedWord("first", 10, 10, 12, 10),
                PositionedWord("second", 11.5, 11.5, 12, 10),
            ]
        )
        index = _coordinate_word_index(words)
        targets = [
            PositionedWord("target", 10, 10, 1, 1),
            PositionedWord("target", 11.5, 11.5, 1, 1),
            PositionedWord("target", 200, 200, 1, 1),
        ]
        targets.extend(
            PositionedWord(
                "query",
                randomizer.uniform(-100, 100),
                randomizer.uniform(-100, 100),
                1,
                1,
            )
            for _ in range(250)
        )

        for target in targets:
            expected = next((
                word_index
                for word_index, word in enumerate(words)
                if abs(word.left - target.left) <= 1.5
                and abs(word.top - target.top) <= 1.5
            ), None)
            self.assertEqual(
                _first_coordinate_word_match(target, words, index),
                expected,
            )

    def test_indexed_table_word_merge_matches_the_legacy_full_scan(self):
        randomizer = random.Random(118)
        original = [
            PositionedWord(
                f"word-{index}",
                randomizer.uniform(0, 1000),
                randomizer.uniform(0, 1000),
                randomizer.uniform(4, 60),
                randomizer.uniform(5, 28),
            )
            for index in range(300)
        ]
        requested = [
            PositionedWord(
                word.text,
                word.left + randomizer.uniform(-1.5, 1.5),
                word.top + randomizer.uniform(-1.5, 1.5),
                word.width + randomizer.uniform(-2, 2),
                word.height + randomizer.uniform(-2, 2),
            )
            for word in original[:80]
        ]
        requested.extend(
            PositionedWord(
                f"extra-{index}",
                randomizer.uniform(0, 1000),
                randomizer.uniform(0, 1000),
                randomizer.uniform(4, 60),
                randomizer.uniform(5, 28),
            )
            for index in range(120)
        )
        cells = [[TableCell(0, 1, 0, 1, "", 0, 0, 1000, 1000)]]

        expected = list(requested)
        for word in original:
            center_x = word.left + word.width / 2
            center_y = word.top + word.height / 2
            inside_table = any(
                cell.left <= center_x <= cell.right
                and cell.top <= center_y <= cell.bottom
                for row in cells
                for cell in row
                if cell.right > cell.left and cell.bottom > cell.top
            )
            if not inside_table:
                continue
            already_present = any(
                abs(candidate.left - word.left) <= 1.5
                and abs(candidate.top - word.top) <= 1.5
                and abs(candidate.width - word.width) <= 2
                and abs(candidate.height - word.height) <= 2
                for candidate in expected
            )
            if not already_present:
                expected.append(
                    PositionedWord(word.text, word.left, word.top, word.width, word.height)
                )

        actual = _merge_coordinate_words(requested, original, cells)

        self.assertEqual(actual, expected)

    def test_indexed_table_cell_membership_matches_the_rectangle_scan(self):
        randomizer = random.Random(419)
        cells = []
        for cell_index in range(150):
            left = randomizer.uniform(-2000, 10000)
            top = randomizer.uniform(-2000, 10000)
            cells.append(
                TableCell(
                    0,
                    1,
                    cell_index,
                    cell_index + 1,
                    "",
                    left,
                    top,
                    left + randomizer.uniform(1, 500),
                    top + randomizer.uniform(1, 500),
                )
            )
        cells.extend(
            [
                TableCell(0, 1, 0, 1, "", -5000, -5000, 5000, 5000),
                TableCell(0, 1, 1, 2, "", float("-inf"), 4, float("inf"), 8),
                TableCell(0, 1, 2, 3, "", 10, 10, 10, 20),
            ]
        )
        rows = [cells]
        index = _table_cell_coordinate_index(rows)
        points = [
            (randomizer.uniform(-3000, 11000), randomizer.uniform(-3000, 11000))
            for _ in range(300)
        ]
        points.extend(
            [
                (0, 0),
                (5000, 5000),
                (10000, 6),
                (float("nan"), 6),
                (10000, float("inf")),
            ]
        )

        for center_x, center_y in points:
            expected = any(
                cell.left <= center_x <= cell.right
                and cell.top <= center_y <= cell.bottom
                for row in rows
                for cell in row
                if cell.right > cell.left and cell.bottom > cell.top
            )
            self.assertEqual(
                _table_contains_center(center_x, center_y, index),
                expected,
            )

    def test_indexed_word_to_cell_membership_matches_the_legacy_nested_scan(self):
        randomizer = random.Random(1207)
        tables = []
        for table_index, cell_count in enumerate((120, 70)):
            cells = []
            for cell_index in range(cell_count):
                left = randomizer.uniform(-1000, 8000)
                top = randomizer.uniform(-1000, 8000)
                cells.append(
                    TableCell(
                        cell_index // 10,
                        cell_index // 10 + 1,
                        cell_index % 10,
                        cell_index % 10 + 1,
                        "",
                        left,
                        top,
                        left + randomizer.uniform(5, 500),
                        top + randomizer.uniform(5, 500),
                    )
                )
            cells.append(
                TableCell(
                    20,
                    21,
                    table_index,
                    table_index + 1,
                    "",
                    -5000,
                    -5000,
                    5000,
                    5000,
                )
            )
            cells.append(
                TableCell(
                    21,
                    22,
                    table_index,
                    table_index + 1,
                    "",
                    float("-inf"),
                    4,
                    float("inf"),
                    8,
                )
            )
            tables.append(cells)

        words = [
            PositionedWord(
                "" if index % 4 == 0 else f"word-{index}",
                randomizer.uniform(-3000, 9000),
                randomizer.uniform(-3000, 9000),
                randomizer.uniform(1, 50),
                randomizer.uniform(1, 40),
            )
            for index in range(350)
        ]
        words.append(PositionedWord("edge", float("inf"), 5, 0, 0))

        for require_text in (False, True):
            expected = set()
            for table_index, cells in enumerate(tables):
                for cell in cells:
                    if not (cell.right > cell.left and cell.bottom > cell.top):
                        continue
                    for word in words:
                        if require_text and not clean_ocr_text(word.text):
                            continue
                        center_x = word.left + word.width / 2.0
                        center_y = word.top + word.height / 2.0
                        if (
                            cell.left <= center_x <= cell.right
                            and cell.top <= center_y <= cell.bottom
                        ):
                            expected.add(
                                (
                                    table_index,
                                    cell.row_start,
                                    cell.col_start,
                                    id(cell),
                                )
                            )

            self.assertEqual(
                _table_cell_memberships(tables, words, require_text=require_text),
                expected,
            )

    def test_center_alignment_index_preserves_first_overlapping_cell(self):
        first_large_cell = TableCell(0, 1, 0, 1, "", -45, -10000, 55, 10000)
        second_centered_cell = TableCell(1, 2, 0, 1, "", -95, -1000, 105, 1000)
        cells = [[first_large_cell, second_centered_cell]]
        result = OcrResult(table_cells=cells)
        word = PositionedWord("内容", -50, -5, 110, 10)
        index = _table_cell_order_index(cells)

        candidates = _table_cells_ordered_at_center(5, 0, index)

        self.assertEqual(
            [id(cell) for cell in candidates],
            [id(first_large_cell), id(second_centered_cell)],
        )
        self.assertFalse(_is_centered_table_word(result, word, index))

    def test_cell_word_grouping_matches_legacy_scan_including_degenerate_cells(self):
        tables = [
            [
                TableCell(0, 1, 0, 1, "", 0, 0, 50, 50),
                TableCell(1, 2, 0, 1, "", 10, 10, 10, 30),
                TableCell(2, 3, 0, 1, "", 20, 20, 40, 20),
                TableCell(3, 4, 0, 1, "", 30, 30, 20, 40),
            ],
            [
                TableCell(0, 1, 0, 1, "", 5, 5, 45, 45),
                TableCell(1, 2, 0, 1, "", float("-inf"), 12, float("inf"), 18),
            ],
        ]
        words = [
            PositionedWord("普通词", 8, 8, 4, 4),
            PositionedWord("零宽边界", 10, 12, 0, 2),
            PositionedWord("零高边界", 28, 20, 4, 0),
            PositionedWord("", 15, 15, 4, 4),
            PositionedWord("跨表格词", 20, 14, 4, 2),
        ]

        expected = {}
        for cells in tables:
            for cell in cells:
                for word in words:
                    if not clean_ocr_text(word.text):
                        continue
                    center_x = word.left + word.width / 2.0
                    center_y = word.top + word.height / 2.0
                    if (
                        cell.left <= center_x <= cell.right
                        and cell.top <= center_y <= cell.bottom
                    ):
                        expected.setdefault(id(cell), []).append(word)

        self.assertEqual(
            _positioned_words_by_table_cell(tables, words),
            expected,
        )

    def test_native_cell_text_reuses_indexed_words_and_cleaned_text(self):
        class CountedWords(list):
            scans = 0

            def __iter__(self):
                self.scans += 1
                return super().__iter__()

        cells = []
        words = []
        for column in range(4):
            left = column * 100
            first, second = (f"({column}甲)", f"({column}乙)")
            cells.append(
                TableCell(
                    0,
                    1,
                    column,
                    column + 1,
                    f"{first} {second}",
                    left,
                    0,
                    left + 100,
                    40,
                )
            )
            words.extend((
                PositionedWord(first, left + 5, 10, 30, 12),
                PositionedWord(second, left + 42, 10, 30, 12),
            ))
        result = OcrResult(positioned_words=CountedWords(words))

        expected = [
            _native_cell_text(result, cell, cell.text)
            for cell in cells
        ]
        self.assertEqual(result.positioned_words.scans, len(cells))
        result.positioned_words.scans = 0
        cleaned_text_by_word_id = {}
        with patch(
            "app.services.documents.clean_ocr_text",
            wraps=clean_ocr_text,
        ) as clean_text:
            words_by_cell = _positioned_words_by_table_cell(
                [cells],
                result.positioned_words,
                cleaned_text_by_word_id,
            )
            self.assertEqual(result.positioned_words.scans, 1)
            actual = [
                _native_cell_text(
                    result,
                    cell,
                    cell.text,
                    words_by_cell.get(id(cell), ()),
                    cleaned_text_by_word_id,
                )
                for cell in cells
            ]

        self.assertEqual(actual, expected)
        self.assertEqual(result.positioned_words.scans, 1)
        self.assertEqual(clean_text.call_count, len(words) + len(cells))

    def test_table_cell_origin_index_preserves_first_duplicate_match(self):
        first = TableCell(0, 1, 0, 1, "first")
        duplicate = TableCell(0, 2, 0, 2, "duplicate")
        later = TableCell(0, 1, 1, 2, "later")

        indexed = _table_cells_by_origin([first, duplicate, later])

        self.assertIs(indexed[(0, 0)], first)
        self.assertIs(indexed[(0, 1)], later)
        self.assertEqual(len(indexed), 2)

    def test_transparent_source_images_keep_the_editors_white_page_background(self):
        source = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
        source.putpixel((1, 0), (220, 20, 60, 255))

        flattened = _image_on_white(source)

        self.assertEqual(flattened.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(flattened.getpixel((1, 0)), (220, 20, 60))

    def test_image_pdf_preserves_compatible_source_icc_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "scan.png"
            output_path = Path(directory) / "scan.pdf"
            icc_profile = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")
            ).tobytes()
            Image.new("RGB", (40, 40), (100, 120, 140)).save(
                source_path,
                format="PNG",
                icc_profile=icc_profile,
            )

            _create_image_pdf(source_path, output_path)

            pdf_bytes = output_path.read_bytes()
        self.assertIn(b"ICC_PROFILE", pdf_bytes)
        self.assertIn(icc_profile, pdf_bytes)

    def test_partial_coordinate_edit_keeps_untouched_table_words(self):
        original = [
            PositionedWord("标题", 10, 5, 40, 15),
            PositionedWord("编号", 20, 80, 35, 16),
            PositionedWord("内容", 100, 80, 35, 16),
        ]
        requested = [original[0]]
        cells = [[
            TableCell(0, 1, 0, 1, "编号", 10, 70, 80, 110),
            TableCell(0, 1, 1, 2, "内容", 80, 70, 180, 110),
        ]]
        merged = _merge_coordinate_words(requested, original, cells)
        self.assertEqual(
            [word.text for word in merged],
            ["标题", "编号", "内容"],
        )

    def test_reordered_unchanged_words_are_not_redrawn(self):
        original = PositionedWord("编号", 20, 80, 35, 16)
        reordered = PositionedWord("编号", 20, 80, 35, 16)
        changed = PositionedWord("新编号", 20, 80, 35, 16)
        self.assertTrue(_same_positioned_word(reordered, original))
        self.assertFalse(_same_positioned_word(changed, original))

    def test_positioned_word_change_check_matches_legacy_and_indexes_once(self):
        class CountedWords(list):
            scans = 0

            def __iter__(self):
                self.scans += 1
                return super().__iter__()

        original = CountedWords(
            PositionedWord(f"字段{index}", index * 12, index % 7, 30, 14)
            for index in range(400)
        )
        edited = [
            PositionedWord(word.text, word.left + 0.5, word.top, word.width, word.height)
            for word in reversed(original)
        ]

        legacy_changed = len(original) != len(edited) or any(
            not any(_same_positioned_word(word, candidate) for candidate in original)
            for word in edited
        )
        legacy_scans = original.scans
        original.scans = 0
        indexed_changed = _has_positioned_word_changes(original, edited)

        self.assertEqual(indexed_changed, legacy_changed)
        self.assertEqual(legacy_scans, len(edited))
        self.assertEqual(original.scans, 1)

    def test_edited_short_table_text_keeps_center_alignment(self):
        result = OcrResult(
            table_cells=[[
                TableCell(0, 1, 0, 1, "原编号", 0, 0, 100, 30),
            ]],
        )
        centered = PositionedWord("新编号", 7, 5, 86, 18)
        left_aligned = PositionedWord("说明文字", 5, 5, 20, 18)
        self.assertTrue(_is_centered_table_word(result, centered))
        self.assertFalse(_is_centered_table_word(result, left_aligned))

    def test_expanded_table_word_uses_source_alignment_instead_of_edited_width(self):
        cell = TableCell(0, 1, 0, 1, "原选项", 0, 0, 100, 40)
        source_word = PositionedWord("原选项", 3, 10, 20, 12)
        expanded_word = PositionedWord("编辑后的长选项", 3, 10, 97, 12)
        result = OcrResult(table_cells=[[cell]])

        self.assertTrue(_is_centered_table_word(result, expanded_word))
        self.assertFalse(
            _is_centered_table_word(
                result,
                expanded_word,
                original_words=[source_word],
            )
        )

    def test_table_line_restoration_does_not_reinsert_a_neighbor_row_glyph(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = Path(directory) / "cleared.png"
            image = Image.new("RGB", (120, 70), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 10, 100, 30), outline="black", width=1)
            draw.rectangle((10, 40, 60, 60), outline="black", width=1)
            draw.rectangle((60, 40, 100, 60), outline="black", width=1)
            # A tall character stroke lands on an x-coordinate that is a cell
            # edge in the row below, but not in the edited cell above.
            draw.line((60, 15, 60, 25), fill="black", width=2)
            image.save(source)
            image.close()

            source_word = PositionedWord("旧字", 50, 15, 20, 10)
            edited_word = PositionedWord("新字", 50, 15, 20, 10)
            top_cell = TableCell(0, 1, 0, 1, "新字", 10, 10, 100, 30)
            lower_left = TableCell(1, 2, 0, 1, "", 10, 40, 60, 60)
            lower_right = TableCell(1, 2, 1, 2, "", 60, 40, 100, 60)
            result = OcrResult(
                image_width=120,
                image_height=70,
                positioned_words=[edited_word],
                table_cells=[[top_cell, lower_left, lower_right]],
            )

            _erase_positioned_text(
                result,
                source,
                output,
                words_to_erase=[source_word],
                clear_cells=[top_cell],
            )

            with Image.open(output) as cleared:
                pixels = cleared.convert("RGB")
                self.assertTrue(
                    all(sum(pixels.getpixel((60, y))) > 720 for y in range(15, 26)),
                    "restoring the lower row's vertical border must not restore OCR glyph pixels",
                )
                self.assertLess(sum(pixels.getpixel((60, 45))), 100)

    def test_visual_pdf_background_keeps_untouched_text_when_word_order_changes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "rendered.jpg"
            image = Image.new("RGB", (240, 160), "white")
            pixels = image.load()
            for x in range(30, 70):
                for y in range(40, 55):
                    pixels[x, y] = (0, 0, 0)
            image.save(source, quality=95)
            original = [
                PositionedWord("原文字", 30, 40, 40, 15),
                PositionedWord("标题", 120, 12, 40, 15),
            ]
            result = OcrResult(
                image_width=240,
                image_height=160,
                positioned_words=[original[1], original[0]],
            )
            render_positioned_image(result, source, output, original_words=original)
            from PIL import ImageChops

            difference = ImageChops.difference(
                Image.open(source).convert("RGB"),
                Image.open(output).convert("RGB"),
            )
            # JPEG re-encoding can change a pixel by a tiny amount, but an
            # accidental white textbox would create a large difference.
            # Compare per-channel extrema without materializing every pixel.
            maximum_channel_difference = max(
                channel_max for _, channel_max in difference.getextrema()
            )
            self.assertLessEqual(maximum_channel_difference, 3)

    def test_visual_pdf_centers_edited_heading(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "rendered.jpg"
            Image.new("RGB", (400, 80), "white").save(source, quality=95)
            result = OcrResult(
                image_width=400,
                image_height=80,
                paragraphs=["编辑后的标题"],
                paragraph_styles=["title"],
                positioned_words=[PositionedWord("编辑后的标题", 0, 10, 400, 24)],
            )
            render_positioned_image(
                result,
                source,
                output,
                original_words=[PositionedWord("原标题", 80, 10, 240, 24)],
            )
            rendered = Image.open(output).convert("RGB")
            dark_pixels = [
                (x, y)
                for y in range(0, 45)
                for x in range(rendered.width)
                if sum(rendered.getpixel((x, y))) < 420
            ]
            self.assertTrue(dark_pixels)
            min_x = min(x for x, _ in dark_pixels)
            max_x = max(x for x, _ in dark_pixels)
            self.assertLess(abs((min_x + max_x) / 2 - rendered.width / 2), 12)

    def test_visual_pdf_long_word_does_not_paint_below_its_source_box(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = Path(directory) / "rendered.png"
            Image.new("RGB", (80, 60), "white").save(source)
            original = [PositionedWord("甲", 10, 10, 12, 10)]
            result = OcrResult(
                image_width=80,
                image_height=60,
                positioned_words=[
                    PositionedWord(
                        "这是一个非常长的编辑内容会换成多行但不能盖住下面的文字",
                        10,
                        10,
                        12,
                        10,
                    )
                ],
            )

            render_positioned_image(result, source, output, original_words=original)

            with Image.open(output) as rendered:
                pixels = rendered.convert("RGB").load()
                has_text_inside_box = any(
                    min(pixels[x, y]) < 245
                    for y in range(8, 21)
                    for x in range(rendered.width)
                )
                has_text_below_box = any(
                    min(pixels[x, y]) < 245
                    for y in range(21, rendered.height)
                    for x in range(rendered.width)
                )
                self.assertTrue(has_text_inside_box, "the edited text should remain visible")
                self.assertFalse(
                    has_text_below_box,
                    "overflowing OCR text must not paint over the next source line",
                )

    def test_visual_pdf_wrapped_edit_uses_blank_space_before_next_line(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = Path(directory) / "rendered.png"
            Image.new("RGB", (100, 80), "white").save(source)
            original = [
                PositionedWord("甲", 10, 10, 12, 10),
                PositionedWord("下一行", 10, 40, 40, 10),
            ]
            edited = [
                PositionedWord("这是一个很长的编辑内容需要换行显示", 10, 10, 12, 60),
                original[1],
            ]
            fitted = _fit_coordinate_word_edits(
                original,
                edited,
                [],
                source_width=100,
                source_height=80,
            )
            result = OcrResult(
                image_width=100,
                image_height=80,
                positioned_words=fitted,
            )

            render_positioned_image(result, source, output, original_words=original)

            with Image.open(output) as rendered:
                pixels = rendered.convert("RGB").load()
                self.assertTrue(
                    any(min(pixels[x, y]) < 245 for y in range(21, 39) for x in range(100)),
                    "wrapped edited text should use the blank space below its original OCR box",
                )
                self.assertFalse(
                    any(min(pixels[x, y]) < 245 for y in range(40, 80) for x in range(100)),
                    "wrapped edited text must stop before the next OCR line",
                )

    def test_visual_pdf_centers_edited_table_word_in_its_cell(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "rendered.png"
            image = Image.new("RGB", (240, 100), "white")
            pixels = image.load()
            for x in range(20, 60):
                for y in range(40, 55):
                    pixels[x, y] = (0, 0, 0)
            image.save(source, quality=95)
            original = [PositionedWord("旧编号", 20, 40, 40, 15)]
            result = OcrResult(
                image_width=240,
                image_height=100,
                positioned_words=[PositionedWord("新编号", 20, 40, 40, 15)],
                table_cells=[[
                    TableCell(0, 1, 0, 1, "旧编号", 0, 30, 80, 70),
                ]],
            )
            render_positioned_image(result, source, output, original_words=original)
            rendered = Image.open(output).convert("RGB")
            dark_pixels = [
                (x, y)
                for y in range(30, 70)
                for x in range(0, 100)
                if sum(rendered.getpixel((x, y))) < 420
            ]
            self.assertTrue(dark_pixels)
            min_x = min(x for x, _ in dark_pixels)
            max_x = max(x for x, _ in dark_pixels)
            self.assertLess(abs((min_x + max_x) / 2 - 40), 12)

    def test_partial_table_word_edit_preserves_other_words_in_the_cell(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = Path(directory) / "rendered.png"
            image = Image.new("RGB", (220, 90), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 10, 210, 70), outline=(20, 20, 20), width=1)
            # Stand in for two source OCR words within the same table cell.
            draw.rectangle((30, 30, 52, 42), fill=(20, 20, 20))
            draw.rectangle((100, 30, 128, 42), fill=(20, 20, 20))
            image.save(source)
            original = [
                PositionedWord("保留项", 30, 30, 23, 13),
                PositionedWord("旧值", 100, 30, 29, 13),
            ]
            edited_word = PositionedWord("新值", 100, 30, 29, 13)
            result = OcrResult(
                image_width=220,
                image_height=90,
                positioned_words=[original[0], edited_word],
                table_cells=[[TableCell(0, 1, 0, 1, "保留项新值", 10, 10, 210, 70)]],
            )

            render_positioned_image(result, source, output, original_words=original)

            with Image.open(source) as imported, Image.open(output) as rendered:
                source_pixels = imported.convert("RGB")
                rendered_pixels = rendered.convert("RGB")
                self.assertEqual(
                    rendered_pixels.getpixel((40, 36)),
                    source_pixels.getpixel((40, 36)),
                    "an unchanged OCR word in the edited cell must remain untouched",
                )
                self.assertTrue(
                    any(
                        sum(rendered_pixels.getpixel((x, y))) < 420
                        for y in range(28, 46)
                        for x in range(95, 140)
                    ),
                    "the edited OCR word should still be rendered",
                )

    def test_edited_table_overlays_preserve_textured_paper_in_exports(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "gray-form.png"
            hybrid_background = Path(directory) / "hybrid-background.png"
            rendered_pdf = Path(directory) / "rendered-pdf.png"
            visual_docx = Path(directory) / "visual-edited.docx"
            image = Image.new("RGB", (240, 100))
            pixels = image.load()
            for y in range(image.height):
                for x in range(image.width):
                    base = 170 + x // 8 + y // 16
                    pixels[x, y] = (base, base + 2, base + 4)
            untouched_paper_pixel = image.getpixel((8, 40))
            pdf_untouched_paper_pixel = image.getpixel((15, 38))
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 40, 60, 55), fill=(20, 20, 20))
            # This checkbox mark is in the same cell but outside the OCR text
            # box, so text cleanup must leave it exactly as it was imported.
            draw.rectangle((67, 45, 73, 52), outline=(20, 20, 20), width=1)
            source_mark_pixel = image.getpixel((67, 48))
            image.save(source)
            image.close()
            original_word = PositionedWord("旧编号", 20, 40, 40, 15)
            edited_word = PositionedWord("新编号", 20, 40, 40, 15)
            cell = TableCell(0, 1, 0, 1, "新编号", 0, 30, 80, 70)
            result = OcrResult(
                image_width=240,
                image_height=100,
                positioned_words=[edited_word],
                tables=[[['新编号']]],
                table_cells=[[cell]],
            )

            _create_hybrid_editable_background(result, source, hybrid_background)
            render_positioned_image(
                result,
                source,
                rendered_pdf,
                original_words=[original_word],
            )
            create_docx(result, visual_docx, source, original_words=[original_word])

            with Image.open(hybrid_background) as background:
                self.assertEqual(background.getpixel((8, 40)), untouched_paper_pixel)
                self.assertGreater(min(background.getpixel((30, 45))), 140)
                self.assertEqual(background.getpixel((67, 48)), source_mark_pixel)
            with Image.open(rendered_pdf) as rendered:
                self.assertEqual(rendered.getpixel((15, 38)), pdf_untouched_paper_pixel)
                self.assertGreater(min(rendered.getpixel((30, 45))), 140)
                self.assertEqual(rendered.getpixel((67, 48)), source_mark_pixel)
            with zipfile.ZipFile(visual_docx) as archive:
                media_name = next(
                    name for name in archive.namelist()
                    if name.startswith("word/media/")
                )
                with archive.open(media_name) as media_file:
                    with Image.open(media_file) as background:
                        self.assertEqual(background.getpixel((8, 40)), untouched_paper_pixel)
                        self.assertGreater(min(background.getpixel((30, 45))), 140)
                        self.assertEqual(background.getpixel((67, 48)), source_mark_pixel)

    def test_long_non_table_coordinate_edit_expands_its_text_box(self):
        original = [PositionedWord("学校", 20, 10, 30, 16)]
        edited = [PositionedWord("学校名称", 20, 10, 30, 16)]
        fitted = _fit_coordinate_word_edits(original, edited, [], source_width=200)
        self.assertGreater(fitted[0].width, original[0].width)
        self.assertEqual(fitted[0].left, original[0].left)

    def test_coordinate_edit_preserves_measured_browser_width(self):
        original = [PositionedWord("abcdefghij", 20, 10, 20, 16)]
        # The browser's measured width can differ substantially from a
        # character-count estimate for mixed glyphs and fonts.
        edited = [PositionedWord("MMMMMMMMMMW", 20, 10, 20, 16)]

        fitted = _fit_coordinate_word_edits(
            original,
            edited,
            [],
            source_width=120,
            measured_widths={0: 58},
        )

        self.assertEqual(fitted[0].width, 58)

    def test_coordinate_edit_clamps_measured_width_before_neighbor(self):
        original = [
            PositionedWord("学校", 20, 10, 20, 16),
            PositionedWord("名称", 70, 10, 20, 16),
        ]
        edited = [
            PositionedWord("学校名称很长", 20, 10, 100, 16),
            original[1],
        ]

        fitted = _fit_coordinate_word_edits(
            original,
            edited,
            [],
            source_width=120,
            measured_widths={0: 100},
        )

        self.assertEqual(fitted[0].width, 48)
        self.assertLessEqual(fitted[0].left + fitted[0].width + 1, original[1].left - 1)

    def test_long_coordinate_edit_does_not_cover_neighboring_words(self):
        original = [
            PositionedWord("学校", 20, 10, 30, 16),
            PositionedWord("名称", 52, 10, 30, 16),
        ]
        edited = [
            PositionedWord("学校名称", 20, 10, 30, 16),
            original[1],
        ]

        fitted = _fit_coordinate_word_edits(original, edited, [], source_width=200)

        self.assertEqual(fitted[0].width, original[0].width)
        self.assertLessEqual(fitted[0].left + fitted[0].width + 1, original[1].left - 1)

    def test_long_coordinate_edit_respects_table_and_page_edges(self):
        original = [PositionedWord("学校", 20, 10, 30, 16)]
        edited = [PositionedWord("学校名称", 20, 10, 30, 16)]
        boundary = TableCell(0, 1, 0, 1, "", 52, 0, 100, 40)

        table_fitted = _fit_coordinate_word_edits(
            original,
            edited,
            [[boundary]],
            source_width=200,
        )
        page_fitted = _fit_coordinate_word_edits(
            original,
            edited,
            [],
            source_width=58,
        )

        self.assertLessEqual(
            table_fitted[0].left + table_fitted[0].width + 1,
            boundary.left,
        )
        self.assertLessEqual(page_fitted[0].left + page_fitted[0].width, 58)

    def test_long_table_word_edit_expands_inside_its_source_cell(self):
        original = [PositionedWord("校名", 10, 8, 12, 10)]
        edited = [PositionedWord("学校名称", 10, 8, 12, 10)]
        cell = TableCell(0, 1, 0, 1, "校名", 0, 0, 60, 30)

        fitted = _fit_coordinate_word_edits(
            original,
            edited,
            [[cell]],
            source_width=100,
            source_height=50,
        )

        self.assertGreater(fitted[0].width, original[0].width)
        self.assertLessEqual(fitted[0].left + fitted[0].width, cell.right)

    def test_centered_table_word_edit_stays_centered_in_its_cell(self):
        original = [PositionedWord("编号", 42, 10, 16, 12)]
        edited = [PositionedWord("证件号码", 42, 10, 16, 12)]
        cell = TableCell(0, 1, 0, 1, "编号", 0, 0, 100, 40)

        fitted = _fit_coordinate_word_edits(
            original,
            edited,
            [[cell]],
            source_width=120,
            source_height=60,
        )

        self.assertGreater(fitted[0].width, original[0].width)
        self.assertAlmostEqual(fitted[0].left + fitted[0].width / 2.0, 50.0)
        self.assertGreaterEqual(fitted[0].left, cell.left)
        self.assertLessEqual(fitted[0].left + fitted[0].width, cell.right)

    def test_long_coordinate_edit_can_expand_when_neighbor_is_on_another_line(self):
        original = [
            PositionedWord("学校", 20, 10, 30, 16),
            PositionedWord("下一行", 20, 50, 30, 16),
        ]
        edited = [
            PositionedWord("学校名称", 20, 10, 30, 16),
            original[1],
        ]

        fitted = _fit_coordinate_word_edits(original, edited, [], source_width=200)

        self.assertGreater(fitted[0].width, original[0].width)

    def test_wrapped_coordinate_edit_uses_blank_space_before_next_row(self):
        original = [
            PositionedWord("学校", 20, 10, 30, 10),
            PositionedWord("下一行", 20, 40, 30, 10),
        ]
        edited = [
            PositionedWord("学校名称及详细地址", 20, 10, 30, 80),
            original[1],
        ]

        fitted = _fit_coordinate_word_edits(
            original,
            edited,
            [],
            source_width=200,
            source_height=200,
        )

        self.assertEqual(fitted[0].height, 28)
        self.assertLessEqual(fitted[0].top + fitted[0].height + 1, original[1].top - 1)

    def test_wrapped_coordinate_edit_stops_at_page_bottom(self):
        original = [PositionedWord("学校", 20, 80, 30, 10)]
        edited = [PositionedWord("学校名称及详细地址", 20, 80, 30, 80)]

        fitted = _fit_coordinate_word_edits(
            original,
            edited,
            [],
            source_width=200,
            source_height=100,
        )

        self.assertEqual(fitted[0].height, 19)
        self.assertLessEqual(fitted[0].top + fitted[0].height, 99)

    def test_legacy_table_metadata_still_recognizes_form_title(self):
        result = OcrResult(
            paragraphs=["广东省家庭经济困难学生认定申请表", "（2026年版）"],
            paragraph_styles=[],
            tables=[[['标签', '内容'], ['A', 'B']]],
        )
        self.assertEqual(
            _centered_paragraph_texts(result),
            {"广东省家庭经济困难学生认定申请表"},
        )

    def test_visual_docx_keeps_source_image_without_duplicate_ocr_layer(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "visual.docx"
            Image.new("RGB", (240, 320), "white").save(source, quality=95)
            result = OcrResult(
                image_width=240,
                image_height=320,
                paragraphs=["测试标题"],
                paragraph_styles=["title"],
                positioned_words=[PositionedWord("测试标题", 50, 12, 140, 24)],
                tables=[[['编号', '内容'], ['1', '示例']]],
                table_layouts=[
                    TableLayout(
                        column_widths=[80, 100],
                        row_heights=[40, 80],
                        source_width=240,
                        source_left=20,
                        source_right=220,
                    )
                ],
                table_cells=[
                    [
                        TableCell(0, 1, 0, 1, '编号', 20, 80, 100, 120),
                        TableCell(0, 1, 1, 2, '内容', 100, 80, 220, 120),
                    ]
                ],
            )
            create_docx(result, output, source)

            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                media = [name for name in archive.namelist() if name.startswith("word/media/")]
            self.assertTrue(media)
            # The untouched visual copy must not duplicate OCR text over the
            # scanned image. The separate structured DOCX is the editable
            # cell version.
            self.assertNotIn("w:txbxContent", document_xml)
            self.assertNotIn("测试标题", document_xml)

    def test_visual_docx_does_not_bake_text_and_draw_it_twice_after_edit(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "visual-edited.docx"
            image = Image.new("RGB", (260, 100), "white")
            pixels = image.load()
            for x in range(82, 110):
                for y in range(32, 50):
                    pixels[x, y] = (0, 0, 0)
            image.save(source, quality=95)
            original = [PositionedWord("旧文字", 80, 30, 100, 24)]
            edited = OcrResult(
                image_width=260,
                image_height=100,
                positioned_words=[PositionedWord("新文字", 80, 30, 100, 24)],
                tables=[[['占位']]],
            )
            create_docx(edited, output, source, original_words=original)
            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                media_names = [
                    name for name in archive.namelist() if name.startswith("word/media/")
                ]
                self.assertTrue(media_names)
                background = Image.open(archive.open(media_names[0])).convert("RGB")
            self.assertIn("新文字", document_xml)
            self.assertNotIn("旧文字", document_xml)
            # The background is cleaned; the old OCR glyph must not remain in
            # the embedded image beneath the editable replacement box.
            self.assertGreaterEqual(min(background.getpixel((100, 40))), 245)

    def test_plain_text_scan_edit_keeps_image_layout_and_paper_tone(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "gray-scan.jpg"
            output = Path(directory) / "edited.docx"
            image = Image.new("RGB", (300, 100), (178, 181, 184))
            pixels = image.load()
            for x in range(80, 116):
                for y in range(34, 53):
                    pixels[x, y] = (28, 29, 30)
            image.save(source, quality=98)
            original = [PositionedWord("旧文字", 78, 30, 42, 25)]
            edited = OcrResult(
                image_width=300,
                image_height=100,
                paragraphs=["新文字"],
                positioned_words=[PositionedWord("新文字", 78, 30, 42, 25)],
            )

            create_docx(edited, output, source, original_words=original)

            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                media_names = [
                    name for name in archive.namelist() if name.startswith("word/media/")
                ]
                self.assertTrue(media_names)
                background = Image.open(archive.open(media_names[0])).convert("RGB")
            self.assertIn("w:txbxContent", document_xml)
            self.assertIn("新文字", document_xml)
            self.assertNotIn("旧文字", document_xml)
            repaired_pixel = background.getpixel((96, 42))
            self.assertGreater(min(repaired_pixel), 145)
            self.assertLess(max(repaired_pixel), 215)

    def test_visual_docx_overlays_only_changed_words(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "visual-edited.docx"
            Image.new("RGB", (320, 120), "white").save(source, quality=95)
            original = [
                PositionedWord("未修改文字", 20, 20, 100, 24),
                PositionedWord("旧标题", 160, 20, 80, 24),
            ]
            edited = OcrResult(
                image_width=320,
                image_height=120,
                positioned_words=[
                    PositionedWord("未修改文字", 20, 20, 100, 24),
                    PositionedWord("新标题", 160, 20, 80, 24),
                ],
                tables=[[['占位']]],
            )
            create_docx(edited, output, source, original_words=original)
            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("新标题", document_xml)
            self.assertNotIn("未修改文字", document_xml)
            self.assertNotIn("旧标题", document_xml)

    def test_visual_docx_scales_vertical_labels_by_box_width(self):
        import re
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "visual.docx"
            Image.new("RGB", (1220, 1377), "white").save(source, quality=95)
            result = OcrResult(
                image_width=1220,
                image_height=1377,
                positioned_words=[PositionedWord("学生基本情况", 138, 287, 23, 178)],
                tables=[[["标签", "内容"]]],
            )
            create_docx(
                result,
                output,
                source,
                original_words=[PositionedWord("旧学生基本情况", 138, 287, 23, 178)],
            )
            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("学\n生\n基\n本\n情\n况", document_xml)
            sizes = [int(value) for value in re.findall(r'w:val="(\d+)"', document_xml)]
            self.assertTrue(sizes)
            self.assertLessEqual(max(sizes), 24)

    def test_structured_docx_contains_native_table_and_title(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "structured.docx"
            result = OcrResult(
                paragraphs=["广东省家庭经济困难学生认定申请表"],
                paragraph_styles=["title"],
                tables=[[["姓名", "性别"], ["张三", "男"]]],
                table_layouts=[
                    TableLayout(
                        column_widths=[120, 80],
                        row_heights=[36, 42],
                        source_width=240,
                        source_left=0,
                        source_right=240,
                    )
                ],
                table_cells=[
                    [
                        TableCell(0, 1, 0, 1, "姓名", 0, 0, 120, 36),
                        TableCell(0, 1, 1, 2, "性别", 120, 0, 200, 36),
                        TableCell(1, 2, 0, 1, "张三", 0, 36, 120, 78),
                        TableCell(1, 2, 1, 2, "男", 120, 36, 200, 78),
                    ]
                ],
            )
            create_docx(result, output)

            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")

            self.assertIn("<w:tbl", document_xml)
            self.assertIn("广东省家庭经济困难学生认定申请表", document_xml)
            self.assertIn("姓名", document_xml)
            self.assertIn("张三", document_xml)

    def test_native_table_is_capped_to_word_text_area(self):
        import tempfile

        from docx import Document as WordDocument

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "edge-to-edge.docx"
            result = OcrResult(
                paragraphs=["满幅表格"],
                paragraph_styles=["title"],
                tables=[[["编号", "备注"], ["1", "一等奖"]]],
                table_layouts=[
                    TableLayout(
                        column_widths=[980, 20],
                        row_heights=[30, 40],
                        source_width=1000,
                        source_left=2,
                        source_right=998,
                    )
                ],
            )
            create_docx(result, output)
            document = WordDocument(output)
            section = document.sections[0]
            text_area = section.page_width - section.left_margin - section.right_margin
            table_width = sum(column.width for column in document.tables[0].columns)
            self.assertLessEqual(table_width, text_area + 12700)

    def test_text_only_docx_contains_text_without_table_or_embedded_image(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "text-only.docx"
            result = OcrResult(
                paragraphs=["总结来说，Top-K 只从前 K 名里挑。", "这是第二段文字。"],
                paragraph_styles=["body", "body"],
            )
            create_docx(result, output)

            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                media = [name for name in archive.namelist() if name.startswith("word/media/")]

            self.assertNotIn("<w:tbl", document_xml)
            self.assertEqual(media, [])
            self.assertIn("总结来说，Top-K 只从前 K 名里挑。", document_xml)
            self.assertIn("这是第二段文字。", document_xml)

    def test_text_only_docx_does_not_turn_word_gaps_into_underlines(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "prose.docx"
            result = OcrResult(
                paragraphs=["这是一个长度明显超过标题阈值的普通文字段落，不应该被当成标题。"],
                paragraph_styles=["title"],
                paragraph_layouts=[
                    ParagraphLayout(
                        fragments=[TextFragment("这是一个长度明显超过标题阈值的普通文字段落，不应该被当成标题。", 10, 10, 240, 20)],
                        source_width=800,
                        left=10,
                        top=10,
                        right=250,
                        bottom=30,
                    )
                ],
            )
            create_docx(result, output)
            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")

            self.assertIn("这是一个长度明显超过标题阈值的普通文字段落，不应该被当成标题。", document_xml)
            self.assertNotIn("_", document_xml)
            self.assertNotIn("w:jc w:val=\"center\"", document_xml)

    def test_hybrid_table_docx_keeps_scan_background_and_native_editable_cells(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            output = Path(directory) / "hybrid.docx"
            Image.new("RGB", (240, 120), "white").save(source, quality=95)
            result = OcrResult(
                image_width=240,
                image_height=120,
                paragraphs=["测试标题"],
                positioned_words=[PositionedWord("测试标题", 60, 2, 120, 12)],
                tables=[[['编号', '名称'], ['1', '示例']]],
                table_layouts=[
                    TableLayout(
                        column_widths=[100, 100],
                        row_heights=[30, 50],
                        source_width=240,
                        source_left=20,
                        source_right=220,
                    )
                ],
                table_cells=[[
                    TableCell(0, 1, 0, 1, "编号", 20, 20, 120, 50),
                    TableCell(0, 1, 1, 2, "名称", 120, 20, 220, 50),
                    TableCell(1, 2, 0, 1, "1", 20, 50, 120, 100),
                    TableCell(1, 2, 1, 2, "示例", 120, 50, 220, 100),
                ]],
            )
            create_hybrid_editable_docx(result, output, source)

            with zipfile.ZipFile(output) as archive:
                document_xml = archive.read("word/document.xml").decode("utf-8")
                media = [name for name in archive.namelist() if name.startswith("word/media/")]

            self.assertIn("<w:tbl", document_xml)
            self.assertIn("示例", document_xml)
            # The structured table Word must keep the title editable too;
            # leaving it only in the background image would preserve stale
            # title text after an in-app title edit.
            self.assertIn("测试标题", document_xml)
            self.assertIn("w:txbxContent", document_xml)
            self.assertTrue(media)

    def test_zero_byte_storage_file_is_not_treated_as_source_image(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty.jpg"
            non_empty = Path(directory) / "source.jpg"
            empty.touch()
            non_empty.write_bytes(b"jpeg")
            self.assertFalse(is_non_empty_file(empty))
            self.assertTrue(is_non_empty_file(non_empty))

    def test_edge_to_edge_table_uses_narrow_side_margin(self):
        edge_to_edge = OcrResult(
            tables=[[['编号', '备注'], ['1', '一等奖']]],
            table_layouts=[
                TableLayout(
                    column_widths=[1, 1],
                    row_heights=[1, 1],
                    source_width=569,
                    source_left=2,
                    source_right=567,
                )
            ],
        )
        inset_form = OcrResult(
            tables=[[['标签', '内容'], ['A', 'B']]],
            table_layouts=[
                TableLayout(
                    column_widths=[1, 1],
                    row_heights=[1, 1],
                    source_width=1220,
                    source_left=120,
                    source_right=1112,
                )
            ],
        )
        self.assertLess(_native_side_margin(edge_to_edge), _native_side_margin(inset_form))

    def test_tall_label_keeps_vertical_characters(self):
        word = PositionedWord("学生基本情况", 138, 287, 23, 178)
        self.assertEqual(_native_word_text(word), "学\n生\n基\n本\n情\n况")

    def test_normal_word_is_not_split(self):
        word = PositionedWord("身份证号", 670, 168, 126, 52)
        self.assertEqual(_native_word_text(word), "身份证号")

    def test_visual_textbox_font_fits_long_ocr_line(self):
        self.assertLess(
            _editable_textbox_font_size("□原广东省户籍建档立卡家庭学生", 128, 12),
            9,
        )

    def test_native_cell_preserves_large_horizontal_gaps(self):
        cell = TableCell(
            row_start=0,
            row_end=1,
            col_start=0,
            col_end=1,
            text="□第一项 □第二项",
            left=0,
            top=0,
            right=500,
            bottom=40,
        )
        result = OcrResult(
            positioned_words=[
                PositionedWord("□第一项", 10, 8, 70, 15),
                PositionedWord("□第二项", 170, 8, 70, 15),
            ]
        )
        rendered = _native_cell_text(result, cell, cell.text)
        self.assertGreaterEqual(
            rendered.count(" ") + rendered.count("　"),
            3,
            msg=f"expected source gap to survive, got {rendered!r}",
        )

    def test_overlapping_form_spans_are_reduced_to_safe_native_merges(self):
        import tempfile

        cells = [
            TableCell(2, 9, 2, 6, "右侧区域"),
            # This span overlaps the first one and is the tee-shaped region
            # returned by the family-form table OCR response.
            TableCell(3, 8, 1, 6, "特殊群体类型"),
            TableCell(8, 10, 1, 2, "标签"),
        ]
        plan = _native_merge_plan(cells, row_count=17, column_count=6)
        self.assertEqual(
            [(cell.row_start, cell.row_end, cell.col_start, cell.col_end) for cell in plan],
            [(2, 9, 2, 6), (8, 10, 1, 2)],
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "form-structured.docx"
            result = OcrResult(
                paragraphs=["广东省家庭经济困难学生认定申请表"],
                paragraph_styles=["title"],
                tables=[[["" for _ in range(6)] for _ in range(17)]],
                table_layouts=[TableLayout(
                    column_widths=[1] * 6,
                    row_heights=[1] * 17,
                    source_width=6,
                    source_left=0,
                    source_right=6,
                )],
                table_cells=[cells],
            )
            create_docx(result, output)
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 0)

    def test_native_merge_plan_matches_legacy_occupancy_for_random_spans(self):
        def legacy_merge_plan(cells, row_count, column_count):
            candidates = []
            for cell in cells:
                row_start = max(cell.row_start, 0)
                row_end = min(cell.row_end, row_count)
                col_start = max(cell.col_start, 0)
                col_end = min(cell.col_end, column_count)
                if row_end <= row_start or col_end <= col_start:
                    continue
                height = row_end - row_start
                width = col_end - col_start
                if height <= 1 and width <= 1:
                    continue
                candidates.append((height * width, height, width, cell))
            candidates.sort(
                key=lambda item: (
                    -item[0],
                    -item[1],
                    -item[2],
                    item[3].row_start,
                    item[3].col_start,
                    item[3].row_end,
                    item[3].col_end,
                )
            )
            occupied = set()
            accepted = []
            for _, _, _, cell in candidates:
                row_start = max(cell.row_start, 0)
                row_end = min(cell.row_end, row_count)
                col_start = max(cell.col_start, 0)
                col_end = min(cell.col_end, column_count)
                coordinates = {
                    (row, column)
                    for row in range(row_start, row_end)
                    for column in range(col_start, col_end)
                }
                if coordinates & occupied:
                    continue
                occupied.update(coordinates)
                accepted.append(cell)
            return accepted

        randomizer = random.Random(2509)
        for case in range(12):
            cells = []
            for _ in range(60):
                row_start = randomizer.randint(-3, 22)
                col_start = randomizer.randint(-4, 34)
                cells.append(
                    TableCell(
                        row_start,
                        row_start + randomizer.randint(0, 12),
                        col_start,
                        col_start + randomizer.randint(0, 15),
                    )
                )
            with self.subTest(case=case):
                self.assertEqual(
                    _native_merge_plan(cells, row_count=20, column_count=30),
                    legacy_merge_plan(cells, row_count=20, column_count=30),
                )


if __name__ == "__main__":
    unittest.main()
