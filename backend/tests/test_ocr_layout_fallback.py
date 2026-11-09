import asyncio
import random
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import ocr


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAsyncClient:
    payload = {}

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return FakeResponse(self.payload)


class OcrLayoutFallbackTests(unittest.TestCase):
    @staticmethod
    def legacy_first_line_match(line_tops, top, tolerance, *, line_tops_are_ordered=True):
        del line_tops_are_ordered
        return next(
            (
                index
                for index, line_top in enumerate(line_tops)
                if abs(top - line_top) <= tolerance
            ),
            None,
        )

    @staticmethod
    def legacy_comparable_text_index(paragraph, _lengths, candidates_by_length, _cache):
        entries = sorted(
            entry
            for group in candidates_by_length.values()
            for entry in group
        )
        return next(
            (
                index
                for index, existing in entries
                if ocr._same_ocr_text(paragraph, existing)
            ),
            None,
        )

    @staticmethod
    def legacy_join_clean_ocr_lines(lines):
        result = ""
        for line in lines:
            cleaned = ocr.clean_ocr_text(line)
            if not cleaned:
                continue
            if (
                result
                and result[-1].isascii()
                and result[-1].isalnum()
                and cleaned[0].isascii()
                and cleaned[0].isalnum()
            ):
                result += " "
            result += cleaned
        return ocr.clean_ocr_text(result.strip())

    def test_joining_precleaned_words_matches_legacy_normalize_and_join(self):
        samples = [
            ["比", "Top-K", "进一步提升"],
            ["达到", "p", "的", "阈值"],
            ["abc", "123", "def"],
            ["□学生", "家庭经济困难", "申请表"],
            ["(表格)", " /", "字段"],
            ["ä½ å¥½", "，", "世界"],
        ]
        for lines in samples:
            cleaned = [ocr.clean_ocr_text(line) for line in lines]
            with self.subTest(lines=lines):
                self.assertEqual(
                    ocr._join_clean_ocr_lines(cleaned),
                    self.legacy_join_clean_ocr_lines(cleaned),
                )

    def test_table_header_fragments_clean_each_word_once_and_keep_raw_line_text(self):
        words = [
            ("标题", {"left": 10, "top": 10, "width": 24, "height": 12}),
            ("(表格)", {"left": 38, "top": 10, "width": 36, "height": 12}),
        ]
        body = [{
            "cell_location": [
                {"x": 0, "y": 100},
                {"x": 300, "y": 100},
                {"x": 300, "y": 300},
                {"x": 0, "y": 300},
            ],
        }]

        result = ocr._paragraph_blocks_outside_tables(
            [[ocr.TableCell(0, 1, 0, 1, "", 0, 100, 300, 300)]],
            words,
            image_width=300,
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "标题（表格）")
        self.assertEqual(
            [fragment.text for fragment in result[0][3].fragments],
            ["标题", "（表格）"],
        )

    def test_vertical_line_index_keeps_first_match_and_tolerance_boundaries(self):
        line_tops = [10.0, 20.0, 30.0]
        self.assertEqual(
            ocr._first_line_within_vertical_tolerance(line_tops, 20.0, 10.0),
            0,
        )
        self.assertEqual(
            ocr._first_line_within_vertical_tolerance(line_tops, 20.0, 9.999),
            1,
        )
        self.assertIsNone(
            ocr._first_line_within_vertical_tolerance(line_tops, 100.0, 2.0),
        )
        self.assertEqual(
            ocr._first_line_within_vertical_tolerance(
                [100.0, 20.0],
                20.0,
                2.0,
                line_tops_are_ordered=False,
            ),
            1,
        )

    def test_paragraph_line_index_matches_legacy_grouping_on_dense_scans(self):
        randomizer = random.Random(1609)
        words = []
        for line in range(75):
            top = line * 11 + randomizer.randint(-3, 3)
            for column in range(randomizer.randint(3, 9)):
                words.append((
                    f"字段{line:03d}{column:02d}",
                    {
                        "left": column * 110 + randomizer.randint(-4, 4),
                        "top": top + randomizer.randint(0, 3),
                        "width": randomizer.randint(35, 105),
                        "height": randomizer.choice([8, 10, 14, 22, 30]),
                    },
                ))

        actual = ocr._positioned_paragraph_candidates(words, image_width=900)
        with patch.object(
            ocr,
            "_first_line_within_vertical_tolerance",
            side_effect=self.legacy_first_line_match,
        ), patch.object(
            ocr,
            "_join_clean_ocr_lines",
            side_effect=self.legacy_join_clean_ocr_lines,
        ):
            expected = ocr._positioned_paragraph_candidates(words, image_width=900)

        self.assertEqual(actual, expected)

    def test_paragraph_line_index_falls_back_when_sort_keys_are_numeric_strings(self):
        words = [
            ("上方", {"left": 8, "top": "100", "width": 80, "height": 12}),
            ("第二行甲", {"left": 8, "top": "20", "width": 70, "height": 12}),
            ("第二行乙", {"left": 85, "top": "30", "width": 70, "height": 12}),
        ]

        actual = ocr._positioned_paragraph_candidates(words, image_width=300)
        with patch.object(
            ocr,
            "_first_line_within_vertical_tolerance",
            side_effect=self.legacy_first_line_match,
        ):
            expected = ocr._positioned_paragraph_candidates(words, image_width=300)

        self.assertEqual(actual, expected)

    def test_cell_line_index_matches_legacy_grouping_with_nonfinite_geometry(self):
        randomizer = random.Random(1709)
        words = []
        for line in range(55):
            top = line * 9 + randomizer.randint(-2, 2)
            for column in range(randomizer.randint(2, 7)):
                words.append((
                    f"单元{line:03d}{column:02d}",
                    {
                        "left": column * 80 + randomizer.randint(-3, 3),
                        "top": top + randomizer.randint(0, 4),
                        "width": randomizer.randint(20, 75),
                        "height": randomizer.choice([7, 11, 16, 24]),
                    },
                ))
        # Non-finite coordinates disable binary search for the rest of this
        # batch, preserving the legacy handling of unusual OCR geometry.
        words.extend([
            ("边界", {"left": 15, "top": float("-inf"), "width": 20, "height": 10}),
        ])

        actual = ocr._join_positioned_cell_words(words)
        with patch.object(
            ocr,
            "_first_line_within_vertical_tolerance",
            side_effect=self.legacy_first_line_match,
        ):
            expected = ocr._join_positioned_cell_words(words)

        self.assertEqual(actual, expected)

    def test_positioned_words_are_grouped_by_spatially_indexed_cells(self):
        cells = [
            ocr.TableCell(0, 1, 0, 1, "普通", -64, -64, 64, 64),
            ocr.TableCell(1, 2, 0, 1, "零宽", 128, 0, 128, 100),
            ocr.TableCell(2, 3, 0, 1, "零高", 0, 128, 100, 128),
            ocr.TableCell(3, 4, 0, 1, "反向", 30, 30, 20, 40),
            ocr.TableCell(4, 5, 0, 1, "超大", -1024, -1024, 1024, 1024),
            ocr.TableCell(5, 6, 0, 1, "无限", float("-inf"), 10, float("inf"), 20),
        ]
        words = [
            ocr.PositionedWord("普通词", 0, 0, 2, 2),
            ocr.PositionedWord("零宽边界", 127, 40, 2, 2),
            ocr.PositionedWord("零高边界", 40, 127, 2, 2),
            ocr.PositionedWord("超大单元格", 500, 500, 2, 2),
            ocr.PositionedWord("非有限边界", 0, 14, 2, 2),
            ocr.PositionedWord("非有限中心", float("inf"), 14, 2, 2),
        ]

        expected = {}
        for cell in cells:
            matches = [
                word for word in words
                if ocr._word_center_in_table_cell(word, cell)
            ]
            if matches:
                expected[id(cell)] = matches

        self.assertEqual(ocr._positioned_words_by_table_cell(cells, words), expected)

    def test_horizontal_span_index_matches_legacy_overlap_scan(self):
        cells = [
            ocr.TableCell(index, index + 1, 0, 1, "", x, y, x + 128, y + 80)
            for index, (x, y) in enumerate(
                ((column * 128, row * 80) for row in range(8) for column in range(10))
            )
        ]
        cells.extend([
            ocr.TableCell(80, 81, 0, 1, "零宽", 128, 0, 128, 80),
            ocr.TableCell(81, 82, 0, 1, "零高", 0, 128, 128, 128),
            ocr.TableCell(82, 83, 0, 1, "超大", -8192, -512, 8192, 512),
            ocr.TableCell(83, 84, 0, 1, "无限", float("-inf"), 0, float("inf"), 40),
            ocr.TableCell(84, 85, 0, 1, "反向", 30, 30, 20, 40),
        ])
        spans = [
            (20, 120, 140),
            (128, 127.5, 128.5),
            (128, 500, 36_000),
            (15, float("-inf"), float("inf")),
            (float("nan"), 0, 20),
        ]
        index = ocr._table_cell_spatial_index(cells)

        for center_y, left, right in spans:
            expected = [
                cell for cell in cells
                if (
                    cell.top <= center_y <= cell.bottom
                    and min(right, cell.right) > max(left, cell.left)
                )
            ]
            with self.subTest(center_y=center_y, left=left, right=right):
                self.assertEqual(
                    list(ocr._table_cells_overlapping_horizontal_span(
                        center_y,
                        left,
                        right,
                        index,
                        cells,
                    )),
                    expected,
                )

    def test_positioned_word_overlay_keeps_characters_that_cross_cell_edges(self):
        body = [
            {
                "row_start": 0,
                "col_start": column,
                "cell_location": [
                    {"x": column * 40, "y": 0},
                    {"x": (column + 1) * 40, "y": 20},
                ],
            }
            for column in range(3)
        ]
        grid = [["", "", ""]]

        ocr._overlay_positioned_words(
            grid,
            body,
            [("ABC", {"left": 30, "top": 5, "width": 60, "height": 10})],
        )

        self.assertEqual(grid, [["A", "ABC", "C"]])

    def test_paragraph_candidate_merge_matches_pairwise_fuzzy_reference(self):
        texts = [
            "abcdefghx",
            "abcdefghxyy",
            "abcdefghxyzz",
            "年级",
            "年级班",
            "登记编号已确认",
            "登记编号已确认为准",
            "（申请表）",
            "申请表",
        ] + [f"字段{index:03d}名称与说明" for index in range(40)]
        randomizer = random.Random(407)
        alphabet = "申请表学生信息年级班别编号字段内容说明abcdefghijklmnop"
        for _ in range(45):
            text = "".join(
                randomizer.choice(alphabet)
                for _ in range(randomizer.randint(5, 80))
            )
            texts.append(text)
            if len(text) >= 10:
                position = randomizer.randrange(len(text))
                texts.append(text[:position] + "错" + text[position + 1:])
        preferred = [
            (text, "body", float(index), None)
            for index, text in enumerate(texts)
        ]
        additional = [
            (text, "body", float(100 + index), None)
            for index, text in enumerate(reversed(texts))
        ]

        merged = []
        for candidates in (preferred, additional):
            for candidate in sorted(candidates, key=lambda item: item[2]):
                if not any(
                    ocr._same_ocr_text(candidate[0], existing[0])
                    for existing in merged
                ):
                    merged.append(candidate)
        ordered = sorted(merged, key=lambda item: item[2])
        expected = []
        for index, candidate in enumerate(ordered):
            compact = "".join(candidate[0].split())
            duplicate_fragment = len(compact) <= 4 and any(
                index != other_index
                and compact
                and compact in "".join(other[0].split())
                and len("".join(other[0].split())) > len(compact)
                for other_index, other in enumerate(ordered)
            )
            if not duplicate_fragment:
                expected.append(candidate)

        self.assertEqual(
            ocr._merge_paragraph_candidates(preferred, additional),
            [(text, "body", top, layout) for text, _, top, layout in expected],
        )

    def test_payload_result_dedup_matches_pairwise_reference_incrementally(self):
        first = "家庭经济困难学生认定申请表主要信息填写说明"
        second = "申请人本人承诺信息真实有效并承担相应责任"
        appended = "补充材料清单需要申请人核对无误后提交"
        result_texts = [
            first,
            first[:-1] + "朋",
            appended,
            appended[:3] + "填" + appended[4:],
            second,
            second[:-2] + "责" + second[-1:],
            "另一份互不相关的文档识别结果与字段说明",
        ]
        payload = {
            "words_result": [{"words": first}, {"words": second}],
            "paragraphs_result": [{"words_result_idx": [0]}],
            "results": [{"text": text} for text in result_texts],
        }

        actual = ocr._paragraphs_from_payload(payload)
        with patch.object(
            ocr,
            "_matching_comparable_text_index",
            side_effect=self.legacy_comparable_text_index,
        ):
            expected = ocr._paragraphs_from_payload(payload)

        self.assertEqual(actual, expected)

    def test_paragraph_merge_keeps_the_exact_ninety_percent_length_boundary(self):
        preferred = [
            ("abcdefghx", "body", 1.0, None),
            ("abcdefghxyy", "body", 2.0, None),
            ("abcdefghxyzz", "body", 3.0, None),
        ]

        result = ocr._merge_paragraph_candidates(preferred, [])

        self.assertEqual([item[0] for item in result], ["abcdefghx", "abcdefghxyzz"])

    def test_quick_similarity_gate_matches_the_full_sequence_ratio(self):
        pairs = [
            ("abcdefghx", "abcdefghxyy"),
            ("abcdefghx", "abcdefghxyzz"),
            ("数据库事务锁表单", "数据库事物锁表单"),
            ("一" * 1200 + "甲", "一" * 1200 + "乙"),
            ("完全不同", "abcdefg"),
            ("", "空"),
        ]

        for left, right in pairs:
            expected = bool(
                left
                and right
                and (
                    left == right
                    or ocr.SequenceMatcher(None, left, right).ratio() >= 0.9
                )
            )
            with self.subTest(left=left[:20], right=right[:20]):
                self.assertEqual(ocr._same_comparable_ocr_text(left, right), expected)

    def test_indexed_paragraph_candidate_match_preserves_first_fuzzy_match(self):
        candidates = [
            ("abcdefghx", "body", 1.0, None),
            ("abcdefghxy", "title", 2.0, None),
            ("年级班别学号姓名", "body", 3.0, None),
            ("完全不同的识别内容", "body", 4.0, None),
            ("", "body", 5.0, None),
        ]
        randomizer = random.Random(1809)
        alphabet = "申请表学生信息年级班别编号字段内容说明abcdefghijklmnop"
        for _ in range(80):
            text = "".join(
                randomizer.choice(alphabet)
                for _ in range(randomizer.randint(5, 70))
            )
            candidates.append((text, "body", float(len(candidates)), None))

        (
            candidate_lengths,
            candidates_by_length,
            matcher_cache,
        ) = ocr._index_paragraph_candidates(candidates)
        paragraphs = [
            "abcdefghxy",
            "abcdefghxyy",
            "年级班别学号姓名",
            "完全无关的识别段落",
            *[candidate[0] for candidate in candidates[5:]],
        ]
        for paragraph in paragraphs:
            expected = next(
                (
                    index
                    for index, candidate in enumerate(candidates)
                    if ocr._same_ocr_text(paragraph, candidate[0])
                ),
                None,
            )
            with self.subTest(paragraph=paragraph[:24]):
                self.assertEqual(
                    ocr._matching_comparable_text_index(
                        paragraph,
                        candidate_lengths,
                        candidates_by_length,
                        matcher_cache,
                    ),
                    expected,
                )

    def test_candidate_match_index_matches_legacy_on_random_text_pairs(self):
        randomizer = random.Random(1909)
        alphabet = "学生信息表格编号申请内容说明年级姓名abcdefghijklmnop"
        candidates = []
        for index in range(140):
            text = "".join(
                randomizer.choice(alphabet)
                for _ in range(randomizer.randint(5, 100))
            )
            candidates.append((text, "body", float(index), None))
        for index in range(0, len(candidates), 7):
            text = candidates[index][0]
            if len(text) >= 8:
                position = randomizer.randrange(len(text))
                candidates.append((
                    text[:position] + "错" + text[position + 1:],
                    "body",
                    float(len(candidates)),
                    None,
                ))

        (
            candidate_lengths,
            candidates_by_length,
            matcher_cache,
        ) = ocr._index_paragraph_candidates(candidates)
        for _ in range(120):
            paragraph = randomizer.choice(candidates)[0]
            if len(paragraph) >= 8 and randomizer.random() < 0.5:
                position = randomizer.randrange(len(paragraph))
                paragraph = paragraph[:position] + "误" + paragraph[position + 1:]
            expected = next(
                (
                    index
                    for index, candidate in enumerate(candidates)
                    if ocr._same_ocr_text(paragraph, candidate[0])
                ),
                None,
            )
            self.assertEqual(
                ocr._matching_comparable_text_index(
                    paragraph,
                    candidate_lengths,
                    candidates_by_length,
                    matcher_cache,
                ),
                expected,
            )

    def test_candidate_matcher_cache_stays_bounded_for_dense_pages(self):
        candidates = [
            (f"field{index:04d}text", "body", float(index), None)
            for index in range(700)
        ]
        (
            candidate_lengths,
            candidates_by_length,
            matcher_cache,
        ) = ocr._index_paragraph_candidates(candidates)

        self.assertIsNone(
            ocr._matching_comparable_text_index(
                "zzzzzzzzzzzzzz",
                candidate_lengths,
                candidates_by_length,
                matcher_cache,
            )
        )
        self.assertEqual(len(matcher_cache), ocr._OCR_CANDIDATE_MATCHER_CACHE_SIZE)

    def test_tesseract_tsv_is_normalized_to_bounded_word_boxes(self):
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t-4\t12\t30\t14\t91.5\t标题\n"
            "5\t1\t1\t1\t1\t2\t42\t12\t0\t14\t80\t无效\n"
            "5\t1\t1\t1\t2\t1\t14\t35\t22\t11\t-1\t低置信度\n"
            "5\t1\t1\t1\t2\t2\t40\t35\t26\t11\t87\t正文\n"
        )
        with patch.object(
            ocr.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=tsv),
        ) as run:
            positioned = ocr._tesseract_positioned_words(Path("scan.png"))

        self.assertEqual(
            positioned,
            [
                (
                    "标题",
                    {"left": 0, "top": 12, "width": 26, "height": 14, "confidence": 92},
                ),
                (
                    "正文",
                    {"left": 40, "top": 35, "width": 26, "height": 11, "confidence": 87},
                ),
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 45)

    def test_tesseract_sparse_page_mode_is_selectable(self):
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t20\t22\t28\t14\t91\t姓名\n"
        )
        with patch.object(
            ocr.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=tsv),
        ) as run:
            positioned = ocr._tesseract_positioned_words(
                Path("scan.png"),
                page_segmentation_mode=11,
            )

        self.assertEqual(positioned[0][0], "姓名")
        self.assertIn("--psm", run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("--psm") + 1], "11")

    def test_recovers_missing_table_word_boxes_from_exact_local_ocr(self):
        cell = ocr.TableCell(0, 1, 0, 1, "姓名", 10, 10, 90, 50)
        result = ocr.OcrResult(table_cells=[[cell]])
        local_words = [
            ("姓", {"left": 20, "top": 22, "width": 12, "height": 14}),
            ("名", {"left": 38, "top": 22, "width": 12, "height": 14}),
            ("额外", {"left": 100, "top": 22, "width": 20, "height": 14}),
        ]
        with patch.object(ocr, "_tesseract_positioned_words", return_value=local_words) as fallback:
            ocr._supplement_table_positions_from_local_ocr(result, Path("scan.png"))

        self.assertEqual([word.text for word in result.positioned_words], ["姓", "名"])
        self.assertEqual([word.left for word in result.positioned_words], [20, 38])
        fallback.assert_called_once_with(Path("scan.png"))

    def test_local_table_boxes_keep_provider_punctuation_and_spacing(self):
        cell = ocr.TableCell(0, 1, 0, 1, "姓名（学生）", 10, 10, 90, 50)
        local_words = [
            ("姓名", {"left": 20, "top": 22, "width": 24, "height": 14}),
            ("学生", {"left": 48, "top": 22, "width": 24, "height": 14}),
        ]

        recovered = ocr._recover_table_cell_word_boxes(cell, local_words)

        self.assertEqual([word.text for word in recovered], ["姓名", "（学生）"])

    def test_recovers_boxes_through_a_small_local_ocr_mismatch_using_provider_text(self):
        cell = ocr.TableCell(0, 1, 0, 1, "身份证号码", 10, 10, 190, 50)
        local_words = [
            ("身份证", {"left": 20, "top": 22, "width": 45, "height": 14}),
            ("号吗", {"left": 72, "top": 22, "width": 30, "height": 14}),
        ]

        recovered = ocr._recover_table_cell_word_boxes(cell, local_words)

        self.assertEqual([word.text for word in recovered], ["身份证", "号码"])
        self.assertEqual([word.left for word in recovered], [20, 72])

    def test_keeps_table_fallback_when_local_words_do_not_exactly_match(self):
        cell = ocr.TableCell(0, 1, 0, 1, "姓名", 10, 10, 90, 50)
        result = ocr.OcrResult(table_cells=[[cell]])
        local_words = [("名称", {"left": 20, "top": 22, "width": 28, "height": 14})]
        with patch.object(ocr, "_tesseract_positioned_words", return_value=local_words):
            ocr._supplement_table_positions_from_local_ocr(result, Path("scan.png"))

        self.assertEqual(result.positioned_words, [])

    def test_skips_local_ocr_when_cloud_positions_already_cover_cell_text(self):
        cell = ocr.TableCell(0, 1, 0, 1, "姓名", 10, 10, 90, 50)
        result = ocr.OcrResult(
            table_cells=[[cell]],
            positioned_words=[
                ocr.PositionedWord("姓", 20, 22, 12, 14),
                ocr.PositionedWord("名", 38, 22, 12, 14),
            ],
        )
        with patch.object(ocr, "_tesseract_positioned_words") as fallback:
            ocr._supplement_table_positions_from_local_ocr(result, Path("scan.png"))

        fallback.assert_not_called()
        self.assertEqual([word.text for word in result.positioned_words], ["姓", "名"])

    def test_replaces_partial_table_word_boxes_with_complete_local_alignment(self):
        cell = ocr.TableCell(0, 1, 0, 1, "姓名", 10, 10, 90, 50)
        result = ocr.OcrResult(
            table_cells=[[cell]],
            positioned_words=[ocr.PositionedWord("姓", 20, 22, 12, 14)],
        )
        local_words = [
            ("姓", {"left": 20, "top": 22, "width": 12, "height": 14}),
            ("名", {"left": 38, "top": 22, "width": 12, "height": 14}),
        ]
        with patch.object(ocr, "_tesseract_positioned_words", return_value=local_words):
            ocr._supplement_table_positions_from_local_ocr(result, Path("scan.png"))

        self.assertEqual([word.text for word in result.positioned_words], ["姓", "名"])
        self.assertEqual([word.left for word in result.positioned_words], [20, 38])

    def test_baidu_table_results_use_local_fallback_before_returning(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            Image.new("RGB", (100, 80), "white").save(image_path)
            table_result = ocr.OcrResult(
                tables=[[['姓名']]],
                table_cells=[[ocr.TableCell(0, 1, 0, 1, "姓名", 10, 10, 90, 50)]],
            )
            local_words = [
                ("姓", {"left": 20, "top": 22, "width": 12, "height": 14}),
                ("名", {"left": 38, "top": 22, "width": 12, "height": 14}),
            ]
            with (
                patch.object(ocr.httpx, "AsyncClient", FakeAsyncClient),
                patch.object(ocr.BaiduOcrProvider, "_get_access_token", new=AsyncMock(return_value="token")),
                patch.object(ocr.BaiduOcrProvider, "_recognize_positioned_words", new=AsyncMock(return_value=([], False, None, []))),
                patch.object(ocr.BaiduOcrProvider, "_recognize_table", new=AsyncMock(return_value=table_result)),
                patch.object(ocr, "_refine_table_merges_from_image_lines") as refine,
                patch.object(ocr, "_tesseract_positioned_words", return_value=local_words) as fallback,
            ):
                result = asyncio.run(
                    ocr.BaiduOcrProvider("key", "secret", "https://ocr.test").recognize(image_path)
                )

        self.assertEqual([word.text for word in result.positioned_words], ["姓", "名"])
        refine.assert_called_once_with(table_result, image_path)
        fallback.assert_called_once_with(image_path)

    def test_baidu_text_results_use_sparse_local_boxes_when_default_mode_misses(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            Image.new("RGB", (100, 80), "white").save(image_path)
            FakeAsyncClient.payload = {
                "words_result": [
                    {"words": "云端识别的标题"},
                    {"words": "云端识别的正文"},
                ],
            }
            local_words = [
                ("云端识别的标题", {"left": 8, "top": 10, "width": 50, "height": 14}),
                ("云端识别的正文", {"left": 12, "top": 35, "width": 60, "height": 16}),
            ]
            with (
                patch.object(ocr.httpx, "AsyncClient", FakeAsyncClient),
                patch.object(ocr.BaiduOcrProvider, "_get_access_token", new=AsyncMock(return_value="token")),
                patch.object(ocr.BaiduOcrProvider, "_recognize_positioned_words", new=AsyncMock(return_value=([], False, None, []))),
                patch.object(ocr.BaiduOcrProvider, "_recognize_table", new=AsyncMock(return_value=None)),
                patch.object(
                    ocr,
                    "_tesseract_positioned_words",
                    side_effect=[[("完全不匹配", {"left": 0, "top": 0, "width": 30, "height": 12})], local_words],
                ) as fallback,
                patch.object(ocr, "_table_from_image_lines", return_value=None),
            ):
                result = asyncio.run(
                    ocr.BaiduOcrProvider("key", "secret", "https://ocr.test").recognize(image_path)
                )

        self.assertEqual(result.paragraphs, ["云端识别的标题云端识别的正文"])
        self.assertEqual([word.text for word in result.positioned_words], result.paragraphs)
        self.assertEqual(fallback.call_count, 2)
        self.assertEqual(fallback.call_args_list[1].kwargs["page_segmentation_mode"], 11)

    def test_baidu_positioned_response_skips_local_layout_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            Image.new("RGB", (100, 80), "white").save(image_path)
            FakeAsyncClient.payload = {
                "words_result": [{
                    "words": "云端标题",
                    "location": {"left": 12, "top": 18, "width": 44, "height": 16},
                }],
            }
            with (
                patch.object(ocr.httpx, "AsyncClient", FakeAsyncClient),
                patch.object(ocr.BaiduOcrProvider, "_get_access_token", new=AsyncMock(return_value="token")),
                patch.object(ocr.BaiduOcrProvider, "_recognize_positioned_words", new=AsyncMock(return_value=([], False, None, []))),
                patch.object(ocr.BaiduOcrProvider, "_recognize_table", new=AsyncMock(return_value=None)),
                patch.object(ocr, "_tesseract_layout_for_paragraphs") as local_layout,
                patch.object(ocr, "_table_from_image_lines", return_value=None),
            ):
                result = asyncio.run(
                    ocr.BaiduOcrProvider("key", "secret", "https://ocr.test").recognize(image_path)
                )

        self.assertEqual(result.positioned_words[0].text, "云端标题")
        self.assertEqual(result.positioned_words[0].left, 12)
        local_layout.assert_not_called()

    def test_retries_sparse_tesseract_mode_when_form_rules_hide_cell_text(self):
        cell = ocr.TableCell(0, 1, 0, 1, "姓名", 10, 10, 90, 50)
        result = ocr.OcrResult(table_cells=[[cell]])
        local_words = [("姓名", {"left": 20, "top": 22, "width": 28, "height": 14})]
        with patch.object(
            ocr,
            "_tesseract_positioned_words",
            side_effect=[[], local_words],
        ) as fallback:
            ocr._supplement_table_positions_from_local_ocr(result, Path("scan.png"))

        self.assertEqual([word.text for word in result.positioned_words], ["姓名"])
        self.assertEqual(
            fallback.call_args_list,
            [
                unittest.mock.call(Path("scan.png")),
                unittest.mock.call(Path("scan.png"), page_segmentation_mode=11),
            ],
        )

    def test_text_only_gateway_keeps_paragraphs_without_local_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            Image.new("RGB", (100, 80), "white").save(image_path)
            FakeAsyncClient.payload = {"paragraphs": ["云端识别的标题", "云端识别的正文"]}
            with (
                patch.object(ocr.httpx, "AsyncClient", FakeAsyncClient),
                patch.object(ocr, "_tesseract_layout_for_paragraphs") as local_layout,
            ):
                result = asyncio.run(ocr.GenericHttpOcrProvider("https://ocr.test", None).recognize(image_path))

        self.assertEqual(result.paragraphs, ["云端识别的标题", "云端识别的正文"])
        self.assertEqual(result.positioned_words, [])
        local_layout.assert_not_called()

    def test_text_only_gateway_does_not_run_local_layout_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            Image.new("RGB", (100, 80), "white").save(image_path)
            FakeAsyncClient.payload = {"paragraphs": ["云端识别的标题", "云端识别的正文"]}
            with (
                patch.object(ocr.httpx, "AsyncClient", FakeAsyncClient),
                patch.object(ocr, "_tesseract_layout_for_paragraphs") as local_layout,
            ):
                result = asyncio.run(ocr.GenericHttpOcrProvider("https://ocr.test", None).recognize(image_path))

        self.assertEqual(result.paragraphs, ["云端识别的标题", "云端识别的正文"])
        self.assertEqual(result.positioned_words, [])
        local_layout.assert_not_called()

    def test_local_boxes_are_ignored_when_provider_text_does_not_align(self):
        mapped = ocr._map_paragraphs_to_local_boxes(
            ["云端识别的长段落内容"],
            [("完全无关的本地文字", {"left": 8, "top": 10, "width": 50, "height": 14})],
        )

        self.assertEqual(mapped, [])

    def test_compatibility_glyphs_match_without_rewriting_provider_text(self):
        box = {"left": 8, "top": 10, "width": 50, "height": 14}
        mapped = ocr._map_paragraphs_to_local_boxes(
            ["ＡＢＣ１２３数据库锁"],
            [("ABC123数据库锁", box)],
        )

        self.assertEqual(mapped, [("ＡＢＣ１２３数据库锁", box)])

    def test_exact_repetitive_text_uses_linear_mapping(self):
        repeated_text = "一" * 2001
        box = {"left": 8, "top": 10, "width": 50, "height": 14}
        with patch.object(ocr, "SequenceMatcher") as matcher:
            mapped = ocr._map_paragraphs_to_local_boxes(
                [repeated_text],
                [(repeated_text, box)],
            )

        self.assertEqual(mapped, [(repeated_text, box)])
        matcher.assert_not_called()

    def test_near_repetitive_text_maps_around_one_ocr_error_without_full_match(self):
        provider_text = "一" * 2001
        local_text = "一" * 2000 + "二"
        box = {"left": 8, "top": 10, "width": 50, "height": 14}
        with patch.object(ocr, "SequenceMatcher") as matcher:
            mapped = ocr._map_paragraphs_to_local_boxes(
                [provider_text],
                [(local_text, box)],
            )

        self.assertEqual(mapped, [(provider_text, box)])
        matcher.assert_not_called()

    def test_small_reordered_middle_uses_local_sequence_matching(self):
        prefix = "一" * 1500
        suffix = "二" * 1500
        provider_text = f"{prefix}甲乙{suffix}"
        local_text = f"{prefix}乙甲{suffix}"
        box = {"left": 8, "top": 10, "width": 50, "height": 14}
        with patch.object(ocr, "SequenceMatcher", wraps=ocr.SequenceMatcher) as matcher:
            mapped = ocr._map_paragraphs_to_local_boxes(
                [provider_text],
                [(local_text, box)],
            )

        self.assertEqual(mapped, [(provider_text, box)])
        matcher.assert_called_once()
        self.assertEqual(matcher.call_args.args[1], ["甲", "乙"])
        self.assertEqual(matcher.call_args.args[2], ["乙", "甲"])

    def test_disjoint_repetitive_middle_skips_pathological_alignment(self):
        provider_text = "一" * 2000 + "二" * 2000
        local_text = "二" * 2000 + "一" * 2000
        with patch.object(ocr, "SequenceMatcher") as matcher:
            mapped = ocr._map_paragraphs_to_local_boxes(
                [provider_text],
                [(local_text, {"left": 8, "top": 10, "width": 50, "height": 14})],
            )

        self.assertEqual(mapped, [])
        matcher.assert_not_called()

    def test_text_only_gateway_keeps_reflow_when_local_text_does_not_align(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            Image.new("RGB", (100, 80), "white").save(image_path)
            FakeAsyncClient.payload = {"paragraphs": ["云端识别的长段落内容"]}
            local_words = [("完全无关的本地文字", {"left": 8, "top": 10, "width": 50, "height": 14})]
            with (
                patch.object(ocr.httpx, "AsyncClient", FakeAsyncClient),
                patch.object(ocr, "_tesseract_positioned_words", return_value=local_words),
                patch.object(ocr, "_table_from_image_lines", return_value=None),
            ):
                result = asyncio.run(ocr.GenericHttpOcrProvider("https://ocr.test", None).recognize(image_path))

        self.assertEqual(result.paragraphs, ["云端识别的长段落内容"])
        self.assertEqual(result.positioned_words, [])

    def test_gateway_coordinates_take_precedence_over_local_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "scan.png"
            Image.new("RGB", (100, 80), "white").save(image_path)
            FakeAsyncClient.payload = {
                "paragraphs": ["云端标题"],
                "positioned_words": [{
                    "text": "云端标题",
                    "location": {"left": 12, "top": 18, "width": 44, "height": 16},
                }],
            }
            with (
                patch.object(ocr.httpx, "AsyncClient", FakeAsyncClient),
                patch.object(ocr, "_tesseract_positioned_words") as fallback,
                patch.object(ocr, "_table_from_image_lines", return_value=None),
            ):
                result = asyncio.run(ocr.GenericHttpOcrProvider("https://ocr.test", None).recognize(image_path))

        self.assertEqual(result.positioned_words[0].text, "云端标题")
        self.assertEqual(result.positioned_words[0].left, 12)
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
