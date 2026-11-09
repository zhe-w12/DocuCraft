from __future__ import annotations

import base64
import csv
import io
import logging
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from bisect import bisect_left, bisect_right, insort
from collections import Counter, OrderedDict
from collections.abc import Iterator
from difflib import SequenceMatcher
from functools import lru_cache
from heapq import merge
from itertools import chain
from math import ceil, floor, isfinite
from pathlib import Path
from statistics import median
from typing import Protocol
from urllib.parse import urlencode

import httpx
from PIL import Image, ImageOps

from app.services.image_color import _image_to_srgb_on_white, _thumbnail_ocr_input
from app.services.table_geometry import detect_table_line_guides
from app.services.ocr_providers import GenericHttpOcrProvider
from app.services.ocr_types import (
    OcrLocalFallbackRequired,
    OcrNoTextRecognized,
    OcrResult,
    ParagraphLayout,
    PositionedWord,
    TableCell,
    TableLayout,
    TextFragment,
)


logger = logging.getLogger(__name__)

_MAX_OCR_ALIGNMENT_CANDIDATE_PAIRS = 4_000_000
_MAX_BAIDU_OCR_FORM_BYTES = 4_000_000
_MAX_BAIDU_OCR_RAW_FAST_PATH_BYTES = _MAX_BAIDU_OCR_FORM_BYTES * 3 // 4
_MAX_OCR_TABLE_DIMENSION = 1_000
_MAX_OCR_TABLE_GRID_CELLS = 50_000
_NORMALIZED_OCR_COORDINATE_TOLERANCE = 0.25
_OCR_TEXT_MATCH_RATIO = 0.9
_TABLE_CELL_SPATIAL_INDEX_CELL_SIZE = 128.0
_TABLE_CELL_SPATIAL_INDEX_MAX_BUCKETS = 256
_OCR_CANDIDATE_MATCHER_CACHE_SIZE = 512
_TESSERACT_LOW_CONFIDENCE_THRESHOLD = 82.0
_TESSERACT_OSD_CONFIDENCE_THRESHOLD = 15.0
_TESSERACT_TABLE_RULE_NOISE_THRESHOLD = 60.0
_TESSERACT_SINGLE_LETTER_NOISE_THRESHOLD = 20.0
_TESSERACT_DPI = 300
_MAX_CROPPED_MERGED_CELL_OCR_ATTEMPTS = 2
_TESSERACT_LEPTONICA_OTSU_THRESHOLDING_METHOD = 1
_TESSERACT_ADAPTIVE_THRESHOLDING_METHOD = 2
_OCR_MISREAD_CHECKBOX_RE = re.compile(
    r"(?<![\u3400-\u9fffA-Za-z0-9])口(?=\s*(?:"
    r"脱贫|低保|低收入|特困|残疾|孤儿|享受|男|女|是|否|有|无|"
    r"第一|第二|第三|本科|专科|高中|初中|小学|其他|在校|在读|"
    r"已婚|未婚|已就业|未就业|同意|不同意|健康|患病|"
    r"防止返贫致贫|原广东省户籍建档立卡|民族县|边境县|革命老区县|"
    r"农村个体小型种植户|父母均|父母一方|本人或家庭成员|除父母外|在学人数|"
    r"原集中连片|原其他国家扶贫开发|原省级扶贫开发"
    r"))"
)
_OCR_MISREAD_CHECKBOX_IN_OPTION_LIST_RE = re.compile(
    r"口(?=\s*(?:"
    r"防止返贫致贫|原广东省户籍建档立卡|民族县|边境县|革命老区县|"
    r"农村个体小型种植户|父母均|父母一方|本人或家庭成员|除父母外|在学人数|"
    r"享受|"
    r"原集中连片|原其他国家扶贫开发|原省级扶贫开发|"
    r"特困救助学生|特困职工及其子女|最低生活保障家庭学生|低保边缘|刚性支出困难家庭学生|"
    r"学生本人残疾|孤儿（含事实无人抚养"
    r"))"
)
_OCR_TABLE_RULE_NOISE_RE = re.compile(r"^[A-Za-z]{0,2}[\[\]|_]{1,2}$")


def _table_index(value: object) -> int | None:
    """Parse an OCR table row/column index without truncating malformed values."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not isfinite(value) or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not re.fullmatch(r"[+-]?\d+", normalized):
            return None
        try:
            return int(normalized)
        except (ValueError, OverflowError):
            return None
    return None


def _clean_ocr_text_base(value: object) -> str:
    """Normalize Unicode and remove characters that DOCX cannot store."""
    text = unicodedata.normalize("NFC", str(value))

    # 少数代理层会把 UTF-8 文本错误地按 Latin-1 解码后再返回，例如
    # ``ä½ å¥½``。只在文本明显满足这种特征且可以无损还原时修复。
    mojibake_markers = ("Ã", "Â", "â", "ð", "æ", "å", "ç", "è", "é", "ï")
    if any(marker in text for marker in mojibake_markers):
        try:
            repaired = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        else:
            if repaired.count("�") <= text.count("�"):
                text = repaired

    # XML/DOCX 不允许 NUL 等控制字符；保留换行、回车和制表符。
    text = "".join(
        character
        for character in text
        if character in "\n\r\t" or not unicodedata.category(character).startswith("C")
    )
    return text.strip()


def clean_plain_ocr_text(value: object) -> str:
    """Keep recognized prose characters intact while removing invalid controls."""
    return _clean_ocr_text_base(value)


def _normalize_student_aid_form_ocr_misreads(value: str) -> str:
    """Repair anchored Tesseract confusions in the standard aid-form checklist."""
    has_form_context = "家庭人均年收入" in value or (
        "□" in value
        and any(marker in value for marker in ("父母", "在学人数", "除父母外"))
    )
    if not has_form_context:
        return value

    text = re.sub(
        r"不[舍合]\s*农村种植[尸户]\s*或\s*养殖\s*户",
        "不含农村种植户或养殖户",
        value,
    )
    text = re.sub(
        r"不[舍合全]\s*农村种植[尸户]\s*或\s*养殖\s*(?:户|[Pp])\s*([）)])",
        r"不含农村种植户或养殖户\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"不含\s*农村种植户\s*或\s*养\s*JAP",
        "不含农村种植户或养殖户",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"除父母外购养老人", "除父母外赡养老人", text)
    text = re.sub(
        r"(在学人数\s*2\s*人\s*[（(])\s*BAA\s*([）)])",
        r"\1含本人\2",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(家庭人均年收入\s*[（(])\s*A\s*R\s*B\s*T\s*t\s*([）)])",
        r"\1人民币元\2",
        text,
        flags=re.IGNORECASE,
    )


def clean_ocr_text(value: object) -> str:
    """Clean OCR text and apply the legacy form/table typography fixes."""
    text = _clean_ocr_text_base(value)
    # OCR 偶尔会把英文术语的大小写识别错，例如 TOp-K。这里只规范化
    # 明确的 Top-K/Top-p 词形，不改动普通英文内容。
    text = re.sub(r"(?i)top\s*[-‐‑–—]?\s*k", "Top-K", text)
    text = re.sub(r"(?i)top\s*[-‐‑–—]?\s*p", "Top-p", text)
    # OCR 经常把术语和相邻中文粘在一起（如“比Top-K”“Top-p进一步”）。
    # 只处理这些明确术语及其 K/p 变量，避免把普通编号（如“第1项”）误加空格。
    text = re.sub(
        r"(?<=[\u3400-\u9fff])(?=Top-K|Top-p|K(?=\s*[\u3400-\u9fff])|p(?=\s*[\u3400-\u9fff]))",
        " ",
        text,
    )
    text = re.sub(
        r"(?<=Top-K)(?=[\u3400-\u9fff])|(?<=Top-p)(?=[\u3400-\u9fff])|(?<=[Kp])(?=[\u3400-\u9fff])",
        " ",
        text,
    )
    # 中文表单通常使用全角括号；OCR 常把它们识别成半角括号，
    # 这会让标题和单元格文字层与原图出现肉眼可见的差异。
    if re.search(r"[\u3400-\u9fff]", text) and ("(" in text or ")" in text):
        text = text.replace("(", "（").replace(")", "）")
    # 表格里的“群体”“家庭”偶尔会被识别成“群/体”“家/庭”。
    # 只修复这两类已知误识别，保留“男/女”“是/否”等有意义的斜杠。
    text = re.sub(r"群[／/]体", "群体", text)
    text = re.sub(r"家[／/]庭", "家庭", text)
    # 复选框常被百度 OCR 识别为“口”。仅当后面紧跟常见表单选项时，
    # 才把词项边界的“口”当作方框，避免误改“人口”“口腔”等普通词语。
    text = _OCR_MISREAD_CHECKBOX_RE.sub("□", text)
    # 表格 OCR 有时把连续选项拼成一个文本片段，导致后续复选框前面紧挨
    # 上一个选项的汉字，无法使用上面的词项边界判断。只有同一文本片段
    # 已包含方框，或至少有两个明确的选项起始词时，才修复这类内联标记。
    checkbox_markers = "□☐☑☒✓✔√"
    option_markers = list(_OCR_MISREAD_CHECKBOX_IN_OPTION_LIST_RE.finditer(text))
    if any(marker in text for marker in checkbox_markers) or len(option_markers) >= 2:
        text = _OCR_MISREAD_CHECKBOX_IN_OPTION_LIST_RE.sub("□", text)
    # 表格 OCR 容易把同一行的多个复选项粘在一起；在相邻选项之间
    # 补空格，保持原图中的视觉分组，同时不改变单个复选项的文字。
    text = re.sub(r"(?<=[^\s□])□", " □", text)
    return _normalize_student_aid_form_ocr_misreads(text)


def same_ocr_characters(left: object, right: object) -> bool:
    """Compare OCR text without whitespace while preserving every character."""

    def counts(value: object) -> Counter[str]:
        return Counter(
            character
            for character in clean_ocr_text(value)
            if not character.isspace()
        )

    left_counts = counts(left)
    return bool(left_counts) and left_counts == counts(right)


def reconstruct_positioned_cell_text(words: list[PositionedWord]) -> str:
    """Rebuild cell lines from positioned OCR boxes without stretching words."""

    ordered = sorted(words, key=lambda word: (word.top, word.left))
    lines: list[list[PositionedWord]] = []
    line_tops: list[float] = []
    for word in ordered:
        tolerance = max(float(word.height) * 0.65, 3.0)
        line_index = next(
            (
                index
                for index, line_top in enumerate(line_tops)
                if abs(float(word.top) - line_top) <= tolerance
            ),
            None,
        )
        if line_index is None:
            lines.append([word])
            line_tops.append(float(word.top))
        else:
            lines[line_index].append(word)
            line_tops[line_index] = median(item.top for item in lines[line_index])

    rendered_lines: list[str] = []
    for line in lines:
        pieces: list[str] = []
        for word in sorted(line, key=lambda item: item.left):
            value = clean_ocr_text(word.text)
            if not value:
                continue
            pieces.append(value)
        if pieces:
            rendered_lines.append("".join(pieces))
    return space_known_form_fields("\n".join(rendered_lines))


def has_known_form_fields(value: object) -> bool:
    text = clean_ocr_text(value)
    return any(
        marker in text
        for marker in ("省（自治区）", "县（市、区）", "镇（街道）", "村（居委）")
    )


def space_known_form_fields(value: str) -> str:
    address = re.compile(
        r"省（自治区）\s*市\s*县（市、区）\s*镇（街道）\s*村（居委）\s*（门牌号）"
    )
    return address.sub(
        "省（自治区）　市　县（市、区）　镇（街道）　村（居委）　（门牌号）",
        value,
    )


def reorder_checkbox_options_by_position(
    value: object,
    words: list[PositionedWord],
    *,
    max_gap_spaces: int = 3,
    legacy_continuation_to_last: bool = False,
) -> str:
    """Put checkbox options in their visual left-to-right order when OCR shuffled them."""

    text = clean_ocr_text(value)
    if text.count("□") < 2 or not words:
        return text

    options: list[str] = []
    continuations: list[str] = []
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if "□" not in line:
            if options:
                continuations.append(line)
            continue
        pieces = line.split("□")
        prefix = pieces[0].strip()
        if prefix and options:
            options[-1] += prefix
        for piece in pieces[1:]:
            option = piece.strip()
            if option:
                options.append(option)

    if len(options) < 2:
        return text

    anchors = [_checkbox_option_left(option, words) for option in options]
    if any(anchor is None for anchor in anchors):
        return text
    numeric_anchors = [float(anchor) for anchor in anchors if anchor is not None]
    if any(
        abs(left_anchor - right_anchor) < 12.0
        for index, left_anchor in enumerate(numeric_anchors)
        for right_anchor in numeric_anchors[index + 1 :]
    ):
        return text

    ordered = sorted(zip(numeric_anchors, options), key=lambda item: item[0])
    for continuation in continuations:
        # Table OCR can wrap the end of a rightmost checkbox label back to
        # the left edge of the next line. In this form, “庭学生” completes
        # the preceding “家” in “建档立卡家庭学生”; attaching every loose line
        # to the last OCR option duplicated that suffix after reordering.
        dangling_family_labels = (
            []
            if legacy_continuation_to_last
            else [
                index
                for index, (_, option) in enumerate(ordered)
                if option.endswith("家") and continuation.startswith("庭")
            ]
        )
        if len(dangling_family_labels) == 1:
            option_index = dangling_family_labels[0]
            left, option = ordered[option_index]
            ordered[option_index] = (left, option + continuation)
        else:
            last_anchor, last_option = ordered[-1]
            ordered[-1] = (last_anchor, last_option + continuation)

    source_font_height = median(
        float(word.height) for word in words if float(word.height) > 0
    ) or 1.0
    rendered = [f"□{ordered[0][1]}"]
    for (left, option), (next_left, next_option) in zip(ordered, ordered[1:]):
        character_width = sum(
            1.0 if ord(character) >= 0x2E80 else 0.55
            for character in option
            if not character.isspace()
        )
        estimated_right = left + character_width * source_font_height
        gap_spaces = max(
            1,
            min(
                max_gap_spaces,
                int(round(max(next_left - estimated_right, 0.0) / source_font_height)),
            ),
        )
        rendered.append(f"{'　' * gap_spaces}□{next_option}")
    return "".join(rendered)


def _checkbox_option_left(option: str, words: list[PositionedWord]) -> float | None:
    normalized_option = "".join(character for character in option if not character.isspace())
    candidates: list[tuple[int, int, float]] = []
    prefix_length = min(len(normalized_option), 8)
    for word in words:
        normalized_word = "".join(
            character
            for character in clean_ocr_text(word.text)
            if not character.isspace() and character != "□"
        )
        for size in (4, 3, 2):
            for start in range(max(prefix_length - size + 1, 0)):
                fragment = normalized_option[start : start + size]
                if fragment and fragment in normalized_word:
                    candidates.append((size, -start, float(word.left)))
    if not candidates:
        return None
    best_size = max(candidate[0] for candidate in candidates)
    best_prefix = max(
        candidate[1] for candidate in candidates if candidate[0] == best_size
    )
    return min(
        candidate[2]
        for candidate in candidates
        if candidate[0] == best_size and candidate[1] == best_prefix
    )


def _known_location_checkbox_order(value: str) -> str | None:
    """Restore the printed order of the six household-location checkboxes."""
    labels = (
        "原集中连片特困地区县（不含县城）",
        "原其他国家扶贫开发工作重点县",
        "原省级扶贫开发工作重点县",
        "民族县",
        "边境县",
        "革命老区县",
    )
    return _order_known_checkbox_labels(
        value,
        labels,
        separator="　",
        line_break_after=2,
    )


def _order_known_checkbox_labels(
    value: object,
    labels: tuple[str, ...],
    *,
    separator: str = "　",
    line_break_after: int | None = None,
) -> str | None:
    """Rebuild a known form row in its printed order, keeping each check state."""
    compact = "".join(
        character for character in clean_ocr_text(value) if not character.isspace()
    )
    markers = "□☑☒✓✔√"
    located_options: list[tuple[int, int, str]] = []
    rendered_by_label: dict[str, str] = {}
    for label in labels:
        if compact.count(label) != 1:
            return None
        label_start = compact.index(label)
        if label_start == 0:
            return None
        marker_start = label_start - 1
        if (
            compact[marker_start] == "□"
            and marker_start > 0
            and compact[marker_start - 1] in "✓✔√"
        ):
            marker_start -= 1
        marker = compact[marker_start:label_start]
        if not marker or any(character not in markers for character in marker):
            return None
        rendered_by_label[label] = marker + label
        located_options.append((marker_start, label_start + len(label), label))

    # Only normalize a cell made entirely from the recognized options. This
    # avoids rewriting unrelated prose that happens to mention one of them.
    previous_end = 0
    for start, end, _label in sorted(located_options):
        if start != previous_end:
            return None
        previous_end = end
    if previous_end != len(compact):
        return None

    rendered_options = [rendered_by_label[label] for label in labels]
    if line_break_after is not None and 0 < line_break_after < len(rendered_options):
        return (
            separator.join(rendered_options[:line_break_after])
            + "\n"
            + separator.join(rendered_options[line_break_after:])
        )
    return separator.join(rendered_options)


def _known_special_category_checkbox_order(value: str) -> str | None:
    """Restore the printed order of the special-needs category checkboxes."""
    option_groups = (
        (
            "特困救助学生",
            "特困职工及其子女",
            "最低生活保障家庭学生",
            "低保边缘家庭学生",
        ),
        (
            "学生本人残疾",
            "父母一方为残疾人",
            "本人或家庭成员患重大疾病",
            "父母一方抚养",
        ),
    )
    for labels in option_groups:
        ordered = _order_known_checkbox_labels(value, labels, separator="　")
        if ordered is not None:
            return ordered
    return None


def normalize_table_cell_value(
    value: object,
    words: list[PositionedWord],
) -> str:
    """Normalize known form structures while keeping OCR wording authoritative."""

    text = clean_ocr_text(value)
    location_checkbox_order = _known_location_checkbox_order(text)
    if location_checkbox_order is not None:
        return location_checkbox_order
    special_category_order = _known_special_category_checkbox_order(text)
    if special_category_order is not None:
        return special_category_order
    if text.count("□") >= 2:
        return reorder_checkbox_options_by_position(text, words)
    if has_known_form_fields(text):
        candidate = reconstruct_positioned_cell_text(words)
        if same_ocr_characters(candidate, text):
            return candidate
    return text


def _compact_ocr_sequence(value: object) -> str:
    return "".join(
        character
        for character in clean_ocr_text(value)
        if not character.isspace()
    )


def normalize_table_cell_text(
    result: OcrResult,
    source_table_cells: list[list[TableCell]] | None = None,
    positioned_words: list[PositionedWord] | None = None,
) -> bool:
    """Use source coordinates to restore wrapped cell text and checkbox order."""

    words = result.positioned_words if positioned_words is None else positioned_words
    changed = False
    for table_index, cells in enumerate(result.table_cells):
        baseline_cells = (
            source_table_cells[table_index]
            if source_table_cells and table_index < len(source_table_cells)
            else []
        )
        baseline_by_origin = {
            (cell.row_start, cell.col_start): cell
            for cell in baseline_cells
        }
        grid = result.tables[table_index] if table_index < len(result.tables) else []
        for cell in cells:
            baseline = baseline_by_origin.get((cell.row_start, cell.col_start))
            cell_words = [
                word
                for word in words
                if cell.left <= word.left + word.width / 2.0 <= cell.right
                and cell.top <= word.top + word.height / 2.0 <= cell.bottom
            ]
            source_text = cell.text
            if baseline is not None and clean_ocr_text(cell.text) != clean_ocr_text(baseline.text):
                # Repair only strings produced by the previous automatic
                # coordinate reconstruction. A real user edit stays intact.
                legacy_sequences = {
                    _compact_ocr_sequence(reconstruct_positioned_cell_text(cell_words)),
                    _compact_ocr_sequence(
                        reorder_checkbox_options_by_position(
                            baseline.text,
                            cell_words,
                            max_gap_spaces=24,
                            legacy_continuation_to_last=True,
                        )
                    ),
                }
                if _compact_ocr_sequence(cell.text) not in legacy_sequences:
                    continue
                source_text = baseline.text
            normalized_text = normalize_table_cell_value(source_text, cell_words)
            if normalized_text == cell.text:
                continue
            cell.text = normalized_text
            if (
                grid
                and cell.row_start < len(grid)
                and cell.col_start < len(grid[cell.row_start])
            ):
                grid[cell.row_start][cell.col_start] = normalized_text
            changed = True
    return changed


class OcrProvider(Protocol):
    async def recognize(self, image_path: Path) -> OcrResult:
        """识别图片并返回与文档结构无关的统一结果。"""


class MockOcrProvider:
    async def recognize(self, image_path: Path) -> OcrResult:
        text = os.getenv(
            "MOCK_OCR_TEXT",
            "这是开发环境的占位识别结果，请配置自建 OCR 服务后替换。",
        ).strip()
        return OcrResult(paragraphs=[text] if text else [])


def _encode_baidu_ocr_image(image_path: Path) -> str:
    """Keep Baidu's URL-encoded form below its image-size limit.

    Compress only the OCR copy and preserve its pixel dimensions so returned
    word and table coordinates still match the normalized image. The original
    source file is stored separately for preview and download.
    """

    image_bytes = image_path.read_bytes()
    encoded = _encode_baidu_image_payload(image_bytes)
    if encoded is not None:
        return encoded

    try:
        with Image.open(image_path) as image:
            rgb = image.convert("RGB")
            try:
                # Match the high-resolution text path: keep fine strokes at
                # quality 92 whenever the encoded OCR request still fits.
                for quality in (92, 88, 84, 80, 76, 72, 68):
                    buffer = io.BytesIO()
                    try:
                        rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
                        candidate = buffer.getvalue()
                    finally:
                        buffer.close()
                    encoded = _encode_baidu_image_payload(candidate)
                    if encoded is not None:
                        return encoded
            finally:
                rgb.close()
            # Keep pixel dimensions so returned geometry stays aligned. For
            # noisy scans, grayscale can fit the request limit without
            # shrinking thin strokes that a smaller image would lose.
            grayscale = ImageOps.grayscale(image)
            try:
                for quality in (90, 84, 78, 72, 68):
                    buffer = io.BytesIO()
                    try:
                        grayscale.save(
                            buffer,
                            format="JPEG",
                            quality=quality,
                            optimize=True,
                        )
                        candidate = buffer.getvalue()
                    finally:
                        buffer.close()
                    encoded = _encode_baidu_image_payload(candidate)
                    if encoded is not None:
                        return encoded
            finally:
                grayscale.close()
    except (OSError, ValueError):
        logger.warning("Could not fit OCR image within Baidu's encoded form limit")
    raise OcrLocalFallbackRequired(
        "图片无法压缩到云端 OCR 接口的请求大小限制内"
    )


def _encode_baidu_image_payload(image_bytes: bytes) -> str | None:
    """Return base64 data only when its URL-encoded form fits Baidu's limit."""

    # Base64 alone expands the input by roughly one third. Avoid creating a
    # much larger temporary string when the raw image already cannot fit.
    if len(image_bytes) > _MAX_BAIDU_OCR_RAW_FAST_PATH_BYTES:
        return None
    encoded = base64.b64encode(image_bytes).decode("ascii")
    form_value_length = len(encoded) + 2 * sum(
        encoded.count(character) for character in ("+", "/", "=")
    )
    # Keep a small margin for the other form fields sent with the image.
    if len("image=") + form_value_length + 128 > _MAX_BAIDU_OCR_FORM_BYTES:
        return None
    return encoded


def _baidu_direction_correction(direction: int | None) -> int | None:
    """Map Baidu's reported input direction to Pillow's correction angle."""

    return {
        1: -90,
        2: 180,
        3: 90,
    }.get(direction)


def _baidu_direction_from_payload(payload: dict[str, object]) -> int | None:
    raw_direction = payload.get("direction")
    if isinstance(raw_direction, bool):
        return None
    try:
        direction = int(raw_direction)
    except (TypeError, ValueError, OverflowError):
        return None
    return direction if direction in {0, 1, 2, 3} else None


def _rotate_ocr_image_copy(
    source_path: Path,
    destination_path: Path,
    rotation_degrees: int,
) -> None:
    """Write an EXIF-normalized right-angle rotation without changing pixel scale."""

    transpose = {
        -90: Image.Transpose.ROTATE_270,
        90: Image.Transpose.ROTATE_90,
        180: Image.Transpose.ROTATE_180,
    }.get(rotation_degrees)
    if transpose is None:
        raise ValueError("OCR orientation correction must be a right angle")

    with Image.open(source_path) as source:
        oriented = ImageOps.exif_transpose(source)
        try:
            rotated = oriented.transpose(transpose)
            try:
                image_metadata: dict[str, object] = {}
                icc_profile = oriented.info.get("icc_profile")
                if isinstance(icc_profile, bytes):
                    image_metadata["icc_profile"] = icc_profile
                dpi = oriented.info.get("dpi")
                if (
                    isinstance(dpi, tuple)
                    and len(dpi) == 2
                    and all(isinstance(value, (int, float)) for value in dpi)
                ):
                    image_metadata["dpi"] = dpi

                suffix = destination_path.suffix.lower()
                if suffix in {".jpg", ".jpeg"}:
                    jpeg_image = (
                        rotated
                        if rotated.mode in {"RGB", "L", "CMYK"}
                        else rotated.convert("RGB")
                    )
                    try:
                        save_metadata = (
                            image_metadata
                            if jpeg_image.mode == rotated.mode
                            else {
                                key: value
                                for key, value in image_metadata.items()
                                if key != "icc_profile"
                            }
                        )
                        jpeg_image.save(
                            destination_path,
                            format="JPEG",
                            quality=97,
                            optimize=True,
                            **save_metadata,
                        )
                    finally:
                        if jpeg_image is not rotated:
                            jpeg_image.close()
                else:
                    rotated.save(
                        destination_path,
                        format="PNG",
                        **image_metadata,
                    )
            finally:
                rotated.close()
        finally:
            if oriented is not source:
                oriented.close()


def _encode_baidu_text_source_image(source_path: Path, geometry_path: Path) -> str | None:
    """Prefer the least-lossy source available for text-only OCR."""

    try:
        with Image.open(source_path) as source, Image.open(geometry_path) as geometry:
            source_orientation = source.getexif().get(274, 1)
            if source.format in {"JPEG", "MPO"} and source.mode in {"L", "RGB"}:
                source.draft(source.mode, (4096, 4096))
            ImageOps.exif_transpose(source, in_place=True)
            oriented = source
            try:
                has_alpha = (
                    any(channel.upper() == "A" for channel in oriented.getbands())
                    or "transparency" in oriented.info
                )
                if (
                    oriented.size == geometry.size
                    and source.format in {"JPEG", "PNG"}
                    and source_orientation == 1
                    and not has_alpha
                    and not isinstance(oriented.info.get("icc_profile"), bytes)
                ):
                    if source_path.stat().st_size <= _MAX_BAIDU_OCR_RAW_FAST_PATH_BYTES:
                        source_bytes = source_path.read_bytes()
                        encoded_source = _encode_baidu_image_payload(source_bytes)
                        if encoded_source is not None:
                            return encoded_source
                thumbnail_image, close_thumbnail_image = _thumbnail_ocr_input(
                    oriented,
                    (4096, 4096),
                )
                try:
                    rgb, icc_profile = _image_to_srgb_on_white(
                        thumbnail_image,
                        force_srgb=True,
                    )
                finally:
                    if close_thumbnail_image:
                        thumbnail_image.close()
                try:
                    # Keep a resized lossless source lossless when possible.
                    # Thin printed strokes can blur under JPEG quantization,
                    # especially in screenshots and monochrome scans.
                    if source.format in {"PNG", "BMP", "TIFF"}:
                        buffer = io.BytesIO()
                        try:
                            save_options: dict[str, object] = {
                                "format": "PNG",
                                "optimize": True,
                            }
                            if isinstance(icc_profile, bytes):
                                save_options["icc_profile"] = icc_profile
                            rgb.save(buffer, **save_options)
                            lossless_candidate = buffer.getvalue()
                        finally:
                            buffer.close()
                        encoded_lossless = _encode_baidu_image_payload(lossless_candidate)
                        if encoded_lossless is not None:
                            return encoded_lossless
                    # If quality reduction alone cannot meet the provider
                    # limit, resize incrementally instead of discarding this
                    # higher-resolution source for the normalized 2500px copy.
                    while True:
                        for quality in (92, 88, 84, 80, 76, 72, 68):
                            buffer = io.BytesIO()
                            try:
                                save_options = {
                                    "format": "JPEG",
                                    "quality": quality,
                                    "optimize": True,
                                }
                                if isinstance(icc_profile, bytes):
                                    save_options["icc_profile"] = icc_profile
                                rgb.save(buffer, **save_options)
                                candidate = buffer.getvalue()
                            finally:
                                buffer.close()
                            encoded_candidate = _encode_baidu_image_payload(candidate)
                            if encoded_candidate is not None:
                                return encoded_candidate
                        if max(rgb.size) <= 1:
                            break
                        smaller_size = (
                            max(1, int(rgb.width * 0.85)),
                            max(1, int(rgb.height * 0.85)),
                        )
                        if smaller_size == rgb.size:
                            break
                        rgb.thumbnail(smaller_size, Image.Resampling.LANCZOS)
                finally:
                    rgb.close()
            finally:
                if oriented is not source:
                    oriented.close()
    except (OSError, ValueError):
        logger.warning("Could not prepare high-resolution text OCR input; using normalized image")
    return None


class BaiduOcrProvider:
    """百度通用文字识别适配器。

    百度接口要求先使用 API Key/Secret Key 获取 access_token，再以
    application/x-www-form-urlencoded 提交 Base64 图片。表单编码由 httpx 负责。
    """

    def __init__(self, api_key: str, secret_key: str, endpoint: str) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.endpoint = endpoint
        configured_text_endpoint = os.getenv("BAIDU_OCR_TEXT_ENDPOINT", "").strip()
        default_general_endpoint = "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"
        if configured_text_endpoint:
            self.text_endpoint = configured_text_endpoint
        elif endpoint.rstrip("/") == default_general_endpoint:
            self.text_endpoint = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
        else:
            # Preserve custom gateways unless they explicitly configure a
            # separate high-accuracy text endpoint.
            self.text_endpoint = endpoint
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        response = await client.post(
            "https://aip.baidubce.com/oauth/2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.api_key,
                "client_secret": self.secret_key,
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise httpx.TransportError("Baidu OCR token endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise httpx.TransportError("Baidu OCR token endpoint returned a non-object response")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            error_description = payload.get("error_description") or payload.get("error")
            if not error_description:
                raise httpx.TransportError("Baidu OCR token response is missing an access token")
            raise RuntimeError(f"百度 OCR access_token 获取失败: {error_description}")
        self._access_token = token
        try:
            expires_in = int(payload.get("expires_in", 2592000))
        except (TypeError, ValueError, OverflowError):
            expires_in = 60
        self._token_expires_at = time.time() + max(expires_in - 300, 60)
        return token

    async def recognize(self, image_path: Path) -> OcrResult:
        return await self._recognize(image_path)

    async def recognize_with_text_source(
        self,
        image_path: Path,
        text_source_path: Path,
    ) -> OcrResult:
        return await self._recognize(image_path, text_source_path=text_source_path)

    async def _recognize(
        self,
        image_path: Path,
        *,
        text_source_path: Path | None = None,
        detect_direction: bool = True,
    ) -> OcrResult:
        image_base64 = _encode_baidu_ocr_image(image_path)
        try:
            with Image.open(image_path) as source:
                image_width, image_height = source.size
        except (OSError, ValueError):
            image_width, image_height = 0, 0
        async with httpx.AsyncClient(timeout=90) as client:
            token = await self._get_access_token(client)
            (
                positioned_words,
                low_position_confidence,
                detected_direction,
                position_confidence_examples,
            ) = await self._recognize_positioned_words(
                client,
                token,
                image_base64,
                detect_direction=detect_direction,
                image_width=image_width,
                image_height=image_height,
            )
            correction_degrees = _baidu_direction_correction(detected_direction)
            if correction_degrees is not None and detect_direction:
                return await self._recognize_with_orientation_correction(
                    image_path,
                    text_source_path=text_source_path,
                    correction_degrees=correction_degrees,
                )
            # 表格接口能返回行列和单元格范围；先尝试它，普通图片再回退到通用 OCR。
            table_result = await self._recognize_table(
                client,
                token,
                image_base64,
                image_path,
                positioned_words,
            )
            if table_result is not None and table_result.tables:
                _supplement_table_positions_from_local_ocr(table_result, image_path)
                _refine_table_merges_from_image_lines(table_result, image_path)
                normalize_table_cell_text(table_result)
                table_result.low_text_confidence = (
                    low_position_confidence or bool(position_confidence_examples)
                )
                table_result.low_confidence_examples = position_confidence_examples
                return table_result

            # 表格接口不可用或偶发漏表时，仍使用本地像素线检测恢复一个
            # 动态网格。不能直接退回纯文字，否则标题、空白单元格和表格
            # 边界都会消失，正是手机端之前出现“只剩一列”的原因。
            image_table = _table_from_image_lines(image_path, positioned_words)
            if image_table is not None:
                image_table.low_text_confidence = (
                    low_position_confidence or bool(position_confidence_examples)
                )
                image_table.low_confidence_examples = position_confidence_examples
                return image_table

            # 先用高精度坐标 OCR 检测表格；确认是纯文字后复用同一识别
            # 文本，再由上传流程丢弃坐标并将正文重新排成可读段落。
            paragraph_candidates = _positioned_paragraph_candidates(
                positioned_words,
                image_width=image_width,
                preserve_content=True,
            )
            uses_baidu_accurate_text_endpoint = (
                self.text_endpoint.rstrip("/")
                == "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
            )
            text_image_base64 = image_base64
            high_resolution_text_image: str | None = None
            if text_source_path is not None and uses_baidu_accurate_text_endpoint:
                high_resolution_text_image = _encode_baidu_text_source_image(
                    text_source_path,
                    image_path,
                )
                if high_resolution_text_image is not None:
                    text_image_base64 = high_resolution_text_image
            if paragraph_candidates:
                (
                    _text_payload,
                    text_paragraphs,
                    used_general_text_fallback,
                ) = await self._recognize_plain_text_with_fallback(
                    client,
                    token,
                    text_image_base64,
                    reference_paragraphs=[item[0] for item in paragraph_candidates],
                )
                if detect_direction and detected_direction is None:
                    correction_degrees = _baidu_direction_correction(
                        _baidu_direction_from_payload(_text_payload)
                    )
                    if correction_degrees is not None:
                        return await self._recognize_with_orientation_correction(
                            image_path,
                            text_source_path=text_source_path,
                            correction_degrees=correction_degrees,
                        )
                if text_paragraphs:
                    return OcrResult(
                        image_width=image_width,
                        image_height=image_height,
                        paragraphs=text_paragraphs,
                        paragraph_styles=["body"] * len(text_paragraphs),
                        low_text_confidence=(
                            low_position_confidence
                            or _plain_text_confidence_is_low(_text_payload)
                            or bool(position_confidence_examples)
                            or bool(_low_confidence_text_examples(_text_payload))
                        ),
                        low_confidence_examples=_merge_confidence_examples(
                            position_confidence_examples,
                            _low_confidence_text_examples(_text_payload),
                        ),
                        used_general_text_fallback=used_general_text_fallback,
                    )
                # Once the image is classified as prose, treat coordinates as
                # table-detection support only. Geometry-based line breaks can
                # split sentences when providers divide the same image differently.
                positioned_text = _join_ocr_lines(
                    [item[0] for item in paragraph_candidates],
                    preserve_content=True,
                )
                return OcrResult(
                    image_width=image_width,
                    image_height=image_height,
                    paragraphs=[positioned_text] if positioned_text else [],
                    paragraph_styles=["body"] if positioned_text else [],
                    # The dedicated prose OCR returned no usable paragraphs,
                    # so the export falls back to the coordinate OCR text.
                    # Ask the user to verify this degraded result explicitly.
                    low_text_confidence=bool(positioned_text),
                    low_confidence_examples=position_confidence_examples,
                    used_general_text_fallback=used_general_text_fallback,
                )

            (
                payload,
                paragraphs,
                used_general_text_fallback,
            ) = await self._recognize_plain_text_with_fallback(
                client,
                token,
                text_image_base64,
            )
            if detect_direction and detected_direction is None:
                correction_degrees = _baidu_direction_correction(
                    _baidu_direction_from_payload(payload)
                )
                if correction_degrees is not None:
                    return await self._recognize_with_orientation_correction(
                        image_path,
                        text_source_path=text_source_path,
                        correction_degrees=correction_degrees,
                    )

        payload_words = (
            []
            if high_resolution_text_image is not None
            else _generic_positioned_words(
                payload,
                preserve_content=True,
                image_width=image_width,
                image_height=image_height,
            )
        )
        if payload_words:
            positioned_words = payload_words
            image_table = _table_from_image_lines(image_path, positioned_words)
            if image_table is not None:
                image_table.low_text_confidence = (
                    low_position_confidence
                    or _plain_text_confidence_is_low(payload)
                    or bool(position_confidence_examples)
                    or bool(_low_confidence_text_examples(payload))
                )
                image_table.low_confidence_examples = _merge_confidence_examples(
                    position_confidence_examples,
                    _low_confidence_text_examples(payload),
                )
                return image_table
        elif positioned_words:
            image_table = _table_from_image_lines(image_path, positioned_words)
            if image_table is not None:
                image_table.low_text_confidence = (
                    low_position_confidence
                    or _plain_text_confidence_is_low(payload)
                    or bool(position_confidence_examples)
                    or bool(_low_confidence_text_examples(payload))
                )
                image_table.low_confidence_examples = _merge_confidence_examples(
                    position_confidence_examples,
                    _low_confidence_text_examples(payload),
                )
                return image_table
        else:
            local_layout_words, mapped_paragraphs = _tesseract_layout_for_paragraphs(
                paragraphs,
                image_path,
            )
            if local_layout_words:
                image_table = _table_from_image_lines(image_path, local_layout_words)
                if image_table is not None:
                    image_table.low_text_confidence = (
                        low_position_confidence
                        or _plain_text_confidence_is_low(payload)
                        or bool(position_confidence_examples)
                        or bool(_low_confidence_text_examples(payload))
                    )
                    image_table.low_confidence_examples = _merge_confidence_examples(
                        position_confidence_examples,
                        _low_confidence_text_examples(payload),
                    )
                    return image_table
            positioned_words = mapped_paragraphs
        return OcrResult(
            image_width=image_width,
            image_height=image_height,
            paragraphs=paragraphs,
            paragraph_styles=["body"] * len(paragraphs),
            positioned_words=_as_positioned_word_objects(
                positioned_words,
                preserve_content=True,
            ),
            low_text_confidence=(
                low_position_confidence
                or _plain_text_confidence_is_low(payload)
                or bool(position_confidence_examples)
                or bool(_low_confidence_text_examples(payload))
            ),
            low_confidence_examples=_merge_confidence_examples(
                position_confidence_examples,
                _low_confidence_text_examples(payload),
            ),
            used_general_text_fallback=used_general_text_fallback,
        )

    async def _recognize_with_orientation_correction(
        self,
        image_path: Path,
        *,
        text_source_path: Path | None,
        correction_degrees: int,
    ) -> OcrResult:
        """Re-run OCR on an upright copy so all returned geometry shares one frame."""

        try:
            with tempfile.TemporaryDirectory(prefix="iscan-ocr-upright-") as temporary_dir:
                temporary_root = Path(temporary_dir)
                upright_image_path = temporary_root / "normalized.jpg"
                _rotate_ocr_image_copy(image_path, upright_image_path, correction_degrees)

                upright_text_source_path: Path | None = None
                if text_source_path is not None and text_source_path.is_file():
                    upright_text_source_path = temporary_root / (
                        "text-source" + text_source_path.suffix.lower()
                    )
                    _rotate_ocr_image_copy(
                        text_source_path,
                        upright_text_source_path,
                        correction_degrees,
                    )

                result = await self._recognize(
                    upright_image_path,
                    text_source_path=upright_text_source_path,
                    detect_direction=False,
                )
        except Exception as exc:
            # The upload retry/fallback layer can then keep using the detected
            # orientation if this second OCR pass fails after detection.
            setattr(exc, "_iscan_ocr_orientation_correction_degrees", correction_degrees)
            raise
        result.orientation_correction_degrees = correction_degrees
        return result

    async def _recognize_plain_text_with_fallback(
        self,
        client: httpx.AsyncClient,
        token: str,
        image_base64: str,
        *,
        reference_paragraphs: list[str] | None = None,
    ) -> tuple[dict[str, object], list[str], bool]:
        payload: dict[str, object] = {}
        paragraphs: list[str] = []
        used_general_text_fallback = False
        try:
            payload = await self._recognize_text_payload(
                client,
                token,
                image_base64,
                endpoint=self.text_endpoint,
            )
            paragraphs = _paragraphs_from_payload(payload)
        except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning(
                "High-accuracy plain-text OCR failed (error_type=%s)",
                type(exc).__name__,
            )

        if not paragraphs and self.endpoint.rstrip("/") != self.text_endpoint.rstrip("/"):
            try:
                payload = await self._recognize_text_payload(
                    client,
                    token,
                    image_base64,
                    endpoint=self.endpoint,
                )
                paragraphs = _paragraphs_from_payload(payload)
                used_general_text_fallback = bool(paragraphs)
            except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning(
                    "Configured general plain-text OCR fallback failed (error_type=%s)",
                    type(exc).__name__,
                )
        elif paragraphs and self.endpoint.rstrip("/") != self.text_endpoint.rstrip("/"):
            primary_confidence = _plain_text_confidence(payload)
            disagrees_with_positioned_ocr = (
                bool(reference_paragraphs)
                and _plain_text_similarity(paragraphs, reference_paragraphs) < 0.985
            )
            if _confidence_is_low(primary_confidence) or disagrees_with_positioned_ocr:
                try:
                    alternate_payload = await self._recognize_text_payload(
                        client,
                        token,
                        image_base64,
                        endpoint=self.endpoint,
                    )
                    alternate_paragraphs = _paragraphs_from_payload(alternate_payload)
                    alternate_confidence = _plain_text_confidence(alternate_payload)
                    if (
                        alternate_paragraphs
                        and _prefer_plain_text_ocr_candidate(
                            paragraphs,
                            primary_confidence,
                            alternate_paragraphs,
                            alternate_confidence,
                            reference_paragraphs=reference_paragraphs,
                        )
                    ):
                        payload = alternate_payload
                        paragraphs = alternate_paragraphs
                        used_general_text_fallback = True
                except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
                    logger.warning(
                        "Low-confidence plain-text OCR retry failed (error_type=%s)",
                        type(exc).__name__,
                    )

        if not paragraphs:
            logger.warning(
                "Plain-text OCR endpoints returned no text; using positioned OCR paragraphs"
            )
        return payload, paragraphs, used_general_text_fallback

    async def _recognize_text_payload(
        self,
        client: httpx.AsyncClient,
        token: str,
        image_base64: str,
        *,
        endpoint: str | None = None,
    ) -> dict[str, object]:
        endpoint = (endpoint or self.endpoint).rstrip("/")
        request_url = f"{endpoint}?{urlencode({'access_token': token})}"
        if endpoint.endswith("/accurate_basic"):
            parameters = {
                "image": image_base64,
                "language_type": "CHN_ENG",
                "detect_direction": "true",
                "paragraph": "true",
            }
        else:
            parameters = {
                "image": image_base64,
                "language_type": "CHN_ENG",
                "detect_direction": "true",
                "detect_language": "true",
                "paragraph": "true",
            }
        if "aip.baidubce.com" in endpoint:
            parameters["probability"] = "true"
        response = await client.post(
            request_url,
            data=parameters,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("百度 OCR 返回格式错误")
        if payload.get("error_code"):
            raise RuntimeError(f"百度 OCR 调用失败: {payload.get('error_msg', payload)}")
        return payload

    async def _recognize_table(
        self,
        client: httpx.AsyncClient,
        token: str,
        image_base64: str,
        image_path: Path,
        positioned_words: list[tuple[str, dict[str, int]]],
    ) -> OcrResult | None:
        endpoint = os.getenv(
            "BAIDU_OCR_TABLE_ENDPOINT",
            "https://aip.baidubce.com/rest/2.0/ocr/v1/table",
        ).strip()
        try:
            response = await client.post(
                f"{endpoint}?{urlencode({'access_token': token})}",
                data={
                    "image": image_base64,
                    "table_border": "normal",
                    "cell_contents": "true",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                return None
            if payload.get("error_code"):
                return None
            tables_result = payload.get("tables_result", [])
            if not isinstance(tables_result, list):
                return None
        except (httpx.HTTPError, ValueError, TypeError):
            # 未开通表格识别服务时，不应阻塞普通文字识别。
            return None

        paragraphs: list[str] = []
        paragraph_styles: list[str] = []
        paragraph_layouts: list[ParagraphLayout | None] = []
        all_header_candidates: list[tuple[str, str, float, ParagraphLayout | None]] = []
        tables: list[list[list[str]]] = []
        layouts: list[TableLayout] = []
        table_cells: list[list[TableCell]] = []
        try:
            with Image.open(image_path) as source_image:
                image_width = float(source_image.width)
                image_height = float(source_image.height)
        except OSError:
            image_width = 0.0
            image_height = 0.0
        for table in tables_result:
            if not isinstance(table, dict):
                continue

            # header 是表格外的标题/副标题，例如示例图中的两行标题。
            raw_headers = table.get("header", [])
            headers = sorted(
                [
                    item
                    for item in raw_headers
                    if isinstance(item, dict)
                ] if isinstance(raw_headers, list) else [],
                key=lambda value: _top_of_location(value.get("location")),
            )
            header_candidates: list[tuple[str, str, float, ParagraphLayout | None]] = []
            for header_index, item in enumerate(headers):
                if item.get("words"):
                    text = clean_ocr_text(item["words"])
                    if text:
                        # 通用 OCR 偶尔漏掉表格上方的标题/副标题，但表格
                        # 接口仍会返回 header 的四边形坐标。把这份坐标补进
                        # 定位文字层，标题就不会只能“看见”而不能编辑。
                        header_bounds = _location_bounds(
                            item.get("location"),
                            image_width=image_width,
                            image_height=image_height,
                        )
                        existing_header = next(
                            (
                                location
                                for existing, location in positioned_words
                                if _same_ocr_text(text, existing)
                            ),
                            None,
                        )
                        if header_bounds is None and existing_header is not None:
                            try:
                                left = float(existing_header.get("left", 0))
                                top = float(existing_header.get("top", 0))
                                right = left + float(existing_header.get("width", 0))
                                bottom = top + float(existing_header.get("height", 0))
                            except (TypeError, ValueError):
                                pass
                            else:
                                if right > left and bottom > top:
                                    header_bounds = left, top, right, bottom
                        header_layout = None
                        if header_bounds:
                            left, top, right, bottom = header_bounds
                            fragment_text = clean_ocr_text(text)
                            header_layout = ParagraphLayout(
                                fragments=[
                                    TextFragment(
                                        text=fragment_text,
                                        left=left,
                                        top=top,
                                        width=max(right - left, 1.0),
                                        height=max(bottom - top, 1.0),
                                    )
                                ],
                                source_width=image_width,
                                left=left,
                                top=top,
                                right=right,
                                bottom=bottom,
                            )
                        if header_bounds and existing_header is None:
                            positioned_words.append(
                                (text, _ocr_bounds_to_word_location(header_bounds))
                            )
                        header_candidates.append(
                            (
                                text,
                                "title" if header_index == 0 else "subtitle",
                                float(
                                    header_bounds[1]
                                    if header_bounds
                                    else _top_of_location(item.get("location"))
                                ),
                                header_layout,
                            )
                        )

            raw_body = table.get("body", [])
            if (
                not isinstance(raw_body, list)
                or len(raw_body) > _MAX_OCR_TABLE_GRID_CELLS
            ):
                logger.warning("Ignoring OCR table with an invalid or oversized cell list")
                continue
            body: list[dict[str, object]] = []
            for item in raw_body:
                if not isinstance(item, dict):
                    continue
                row_start = _table_index(item.get("row_start", 0))
                col_start = _table_index(item.get("col_start", 0))
                if row_start is None or col_start is None:
                    continue
                row_end = (
                    _table_index(item["row_end"])
                    if "row_end" in item
                    else row_start + 1
                )
                col_end = (
                    _table_index(item["col_end"])
                    if "col_end" in item
                    else col_start + 1
                )
                if row_end is None or col_end is None:
                    continue
                row_start = max(row_start, 0)
                col_start = max(col_start, 0)
                if (
                    row_end <= row_start
                    or col_end <= col_start
                    or row_end > _MAX_OCR_TABLE_DIMENSION
                    or col_end > _MAX_OCR_TABLE_DIMENSION
                ):
                    continue
                body.append({
                    **item,
                    "row_start": row_start,
                    "row_end": row_end,
                    "col_start": col_start,
                    "col_end": col_end,
                })
            if not body:
                continue
            row_count = max(int(item["row_end"]) for item in body)
            column_count = max(int(item["col_end"]) for item in body)
            # The table endpoint occasionally wraps a plain-text page as a
            # one-column "table". Only keep a real grid; text-only pages must
            # fall through to the normal OCR paragraph result.
            if row_count < 2 or column_count < 2:
                continue
            if row_count * column_count > _MAX_OCR_TABLE_GRID_CELLS:
                logger.warning(
                    "Ignoring oversized OCR table grid (%s rows, %s columns)",
                    row_count,
                    column_count,
                )
                continue
            all_header_candidates.extend(header_candidates)

            grid = [["" for _ in range(column_count)] for _ in range(row_count)]
            table_cell_content_overrides: dict[
                tuple[int, int], list[tuple[str, dict[str, int] | None]]
            ] = {}
            for item in body:
                row_start = max(int(item.get("row_start", 0)), 0)
                row_end = min(int(item.get("row_end", row_start + 1)), row_count)
                col_start = max(int(item.get("col_start", 0)), 0)
                col_end = min(int(item.get("col_end", col_start + 1)), column_count)
                value = clean_ocr_text(item.get("words", ""))
                if row_start >= row_end or col_start >= col_end:
                    continue
                cell_content_lines = _table_cell_content_lines(
                    item,
                    image_width=image_width,
                    image_height=image_height,
                )
                cell_contents = _table_cell_contents_text(cell_content_lines)
                if cell_contents:
                    current_compact = _compact_ocr_sequence(
                        grid[row_start][col_start]
                    )
                    contents_compact = _compact_ocr_sequence(cell_contents)
                    if len(contents_compact) > len(current_compact):
                        value = cell_contents
                        if all(location is not None for _, location in cell_content_lines):
                            table_cell_content_overrides[(row_start, col_start)] = (
                                cell_content_lines
                            )
                    elif (
                        contents_compact == current_compact
                        and cell_contents.count("\n")
                        > grid[row_start][col_start].count("\n")
                    ):
                        value = cell_contents
                # 当前 Word 表格模型是二维单元格；跨行/跨列单元格先将文字放入
                # 左上角，其他位置留空，避免重复文字，后续可再增加合并信息。
                if not grid[row_start][col_start]:
                    grid[row_start][col_start] = value
                elif len(_compact_ocr_sequence(value)) > len(
                    _compact_ocr_sequence(grid[row_start][col_start])
                ):
                    grid[row_start][col_start] = value
            if table_cell_content_overrides:
                positioned_words = _supplement_positioned_words_from_table_contents(
                    body,
                    table_cell_content_overrides,
                    positioned_words,
                    image_width=image_width,
                    image_height=image_height,
                )
            positioned_words = _split_positioned_words_by_table_cells(
                body,
                positioned_words,
                image_width=image_width,
                image_height=image_height,
            )
            # Always run the positioned pass: a table cell may be non-empty but
            # still miss one of its wrapped Chinese lines.
            _overlay_positioned_words(
                grid,
                body,
                positioned_words,
                image_width=image_width,
                image_height=image_height,
            )
            tables.append(grid)
            layout = _layout_from_body(
                body,
                row_count,
                column_count,
                image_path,
                image_width=image_width,
                image_height=image_height,
            )
            layouts.append(layout)
            x_boundaries, y_boundaries = _reconstructed_grid_axes(
                body,
                row_count,
                column_count,
                image_path,
                image_width=image_width,
                image_height=image_height,
            )
            cells: list[TableCell] = []
            for item in body:
                row_start = max(int(item.get("row_start", 0)), 0)
                row_end = min(int(item.get("row_end", row_start + 1)), row_count)
                col_start = max(int(item.get("col_start", 0)), 0)
                col_end = min(int(item.get("col_end", col_start + 1)), column_count)
                if row_start >= row_end or col_start >= col_end:
                    continue
                bounds = _cell_bounds(
                    item,
                    image_width=image_width,
                    image_height=image_height,
                ) or (0.0, 0.0, 0.0, 0.0)
                if (
                    len(x_boundaries) == column_count + 1
                    and len(y_boundaries) == row_count + 1
                    and 0 <= col_start < col_end <= column_count
                    and 0 <= row_start < row_end <= row_count
                ):
                    # OCR supplies the merge ranges/text; the image detector
                    # supplies the actual rectangle edges used by the editor.
                    bounds = (
                        x_boundaries[col_start],
                        y_boundaries[row_start],
                        x_boundaries[col_end],
                        y_boundaries[row_end],
                    )
                cells.append(
                    TableCell(
                        row_start=row_start,
                        row_end=row_end,
                        col_start=col_start,
                        col_end=col_end,
                        text=grid[row_start][col_start],
                        left=bounds[0],
                        top=bounds[1],
                        right=bounds[2],
                        bottom=bounds[3],
                    )
                )
            table_cells.append(cells)

        if table_cells:
            outside_candidates = _paragraph_blocks_outside_tables(
                table_cells,
                positioned_words,
                image_width,
            )
            for text, style, _, layout in _merge_paragraph_candidates(
                all_header_candidates,
                outside_candidates,
            ):
                if not any(_same_ocr_text(text, existing) for existing in paragraphs):
                    paragraphs.append(text)
                    paragraph_styles.append(style)
                    paragraph_layouts.append(layout)

        return OcrResult(
            image_width=int(image_width),
            image_height=int(image_height),
            paragraphs=paragraphs,
            paragraph_styles=paragraph_styles,
            paragraph_layouts=paragraph_layouts,
            positioned_words=_as_positioned_word_objects(positioned_words),
            tables=tables,
            table_layouts=layouts,
            table_cells=table_cells,
        )

    async def _recognize_positioned_words(
        self,
        client: httpx.AsyncClient,
        token: str,
        image_base64: str,
        *,
        detect_direction: bool,
        image_width: int = 0,
        image_height: int = 0,
    ) -> tuple[list[tuple[str, dict[str, int]]], bool, int | None, list[str]]:
        configured_endpoint = os.getenv("BAIDU_OCR_POSITION_ENDPOINT", "").strip()
        endpoints = [configured_endpoint] if configured_endpoint else [
            "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate",
            "https://aip.baidubce.com/rest/2.0/ocr/v1/general",
        ]
        detected_direction: int | None = None
        for endpoint in endpoints:
            try:
                parameters = {
                    "image": image_base64,
                    "language_type": "CHN_ENG",
                    "location": "true",
                }
                if "aip.baidubce.com" in endpoint:
                    parameters["probability"] = "true"
                    if detect_direction:
                        parameters["detect_direction"] = "true"
                response = await client.post(
                    f"{endpoint}?{urlencode({'access_token': token})}",
                    data=parameters,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("error_code"):
                    continue
            except (httpx.HTTPError, ValueError, TypeError):
                continue

            if detect_direction and "aip.baidubce.com" in endpoint:
                direction = _baidu_direction_from_payload(payload)
                if direction is not None:
                    detected_direction = direction

            positioned: list[tuple[str, dict[str, int]]] = []
            for item in payload.get("words_result", []):
                if not isinstance(item, dict) or not item.get("words"):
                    continue
                location = item.get("location")
                if not isinstance(location, dict):
                    continue
                text = clean_plain_ocr_text(item["words"])
                if text:
                    try:
                        bounds = {
                            key: int(location[key])
                            for key in ("left", "top", "width", "height")
                        }
                    except (KeyError, TypeError, ValueError, OverflowError):
                        continue
                    if bounds["width"] <= 0 or bounds["height"] <= 0:
                        continue
                    clipped = _clip_ocr_bounds(
                        (
                            float(bounds["left"]),
                            float(bounds["top"]),
                            float(bounds["left"] + bounds["width"]),
                            float(bounds["top"] + bounds["height"]),
                        ),
                        image_width=image_width,
                        image_height=image_height,
                    )
                    if clipped is None:
                        continue
                    positioned.append((text, _ocr_bounds_to_word_location(clipped)))
            if positioned:
                return (
                    positioned,
                    _plain_text_confidence_is_low(payload),
                    detected_direction,
                    _low_confidence_text_examples(payload),
                )
        return [], False, detected_direction, []


def _as_positioned_word_objects(
    positioned_words: list[tuple[str, dict[str, int]]],
    *,
    preserve_content: bool = False,
) -> list[PositionedWord]:
    result: list[PositionedWord] = []
    clean_text = clean_plain_ocr_text if preserve_content else clean_ocr_text
    for text, location in positioned_words:
        result.append(
            PositionedWord(
                text=clean_text(text),
                left=float(location.get("left", 0)),
                top=float(location.get("top", 0)),
                width=max(float(location.get("width", 0)), 1.0),
                height=max(float(location.get("height", 1)), 1.0),
            )
        )
    return [item for item in result if item.text]


def _table_from_image_lines(
    image_path: Path,
    positioned_words: list[tuple[str, dict[str, int]]],
) -> OcrResult | None:
    """Build a best-effort editable grid from detected image rules.

    The cloud table endpoint is preferred because it knows merged cells. This
    fallback deliberately uses only local image geometry and general OCR
    coordinates, so a temporary table-service failure still produces a real,
    dynamic table instead of a paragraph dump or a fixed template.
    """

    if not positioned_words:
        return None
    try:
        with Image.open(image_path) as source:
            image_width, image_height = source.size
    except (OSError, ValueError):
        return None

    guides = detect_table_line_guides(
        image_path,
        (0.0, 0.0, float(image_width), float(image_height)),
    )
    x_boundaries = _clean_line_boundaries(guides.x, float(image_width))
    y_boundaries = _clean_line_boundaries(guides.y, float(image_height))
    x_boundaries, y_boundaries = _keep_lines_with_table_ink(
        image_path,
        x_boundaries,
        y_boundaries,
    )
    y_boundaries = _trim_empty_short_table_edges(
        x_boundaries,
        y_boundaries,
        positioned_words,
    )
    if len(x_boundaries) < 3 or len(y_boundaries) < 3:
        return None

    table_left, table_right = x_boundaries[0], x_boundaries[-1]
    table_top, table_bottom = y_boundaries[0], y_boundaries[-1]
    table_width = table_right - table_left
    table_height = table_bottom - table_top
    # Avoid treating a few unrelated strokes as a document table. These
    # thresholds still allow a table occupying only the central part of a page.
    if table_width < image_width * 0.25 or table_height < image_height * 0.12:
        return None
    if len(x_boundaries) > 40 or len(y_boundaries) > 80:
        return None

    row_count = len(y_boundaries) - 1
    column_count = len(x_boundaries) - 1
    vertical_rules, horizontal_rules = _table_rule_presence(
        image_path,
        x_boundaries,
        y_boundaries,
    )
    row_spans: list[list[tuple[int, int]]] = []
    for row in range(row_count):
        spans: list[tuple[int, int]] = []
        span_start = 0
        for boundary in range(1, column_count):
            if vertical_rules[row][boundary]:
                spans.append((span_start, boundary))
                span_start = boundary
        spans.append((span_start, column_count))
        row_spans.append(spans)

    cell_regions: list[dict[str, int]] = []
    cell_words: list[list[tuple[str, dict[str, int]]]] = []
    row_cell_indexes: list[dict[tuple[int, int], int]] = []
    previous_row_cells: dict[tuple[int, int], int] = {}
    for row, spans in enumerate(row_spans):
        current_row_cells: dict[tuple[int, int], int] = {}
        for span in spans:
            previous_cell_index = previous_row_cells.get(span)
            if (
                row > 0
                and previous_cell_index is not None
                and not any(horizontal_rules[row][span[0] : span[1]])
            ):
                cell_regions[previous_cell_index]["row_end"] = row + 1
                cell_index = previous_cell_index
            else:
                cell_index = len(cell_regions)
                cell_regions.append(
                    {
                        "row_start": row,
                        "row_end": row + 1,
                        "col_start": span[0],
                        "col_end": span[1],
                    }
                )
                cell_words.append([])
            current_row_cells[span] = cell_index
        row_cell_indexes.append(current_row_cells)
        previous_row_cells = current_row_cells

    outside_words: list[tuple[str, dict[str, int]]] = []
    footer_words: list[tuple[str, dict[str, int]]] = []
    positioned_for_layout: list[tuple[str, dict[str, int]]] = []
    for text, location in positioned_words:
        left = float(location.get("left", 0))
        top = float(location.get("top", 0))
        width = max(float(location.get("width", 1)), 1.0)
        height = max(float(location.get("height", 1)), 1.0)
        center_x = left + width / 2.0
        center_y = top + height / 2.0
        if table_left <= center_x <= table_right and table_top <= center_y <= table_bottom:
            if _is_low_confidence_table_rule_noise(text, location):
                continue
            row = bisect_right(y_boundaries, center_y) - 1
            if not 0 <= row < row_count:
                positioned_for_layout.append((text, location))
                continue

            center_column = bisect_right(x_boundaries, center_x) - 1
            center_span = next(
                (
                    span
                    for span in row_spans[row]
                    if span[0] <= center_column < span[1]
                ),
                None,
            )
            if center_span is None:
                positioned_for_layout.append((text, location))
                continue
            center_cell_index = row_cell_indexes[row][center_span]

            # General OCR may put text from both sides of a real cell rule in
            # one wide box. Split only at the cell spans supported by the
            # visible rules; a missing rule means the adjacent grid slots are
            # one merged cell, so their text must stay together.
            word_right = left + width
            overlapping_spans = [
                span
                for span in row_spans[row]
                if max(left, x_boundaries[span[0]])
                < min(word_right, x_boundaries[span[1]])
            ]
            characters = list(text)
            if len(overlapping_spans) > 1 and characters:
                character_centers = [
                    left + width * (index + 0.5) / len(characters)
                    for index in range(len(characters))
                ]
                assigned_character_indexes: set[int] = set()
                split_words: list[tuple[int, str, dict[str, int]]] = []
                for span in overlapping_spans:
                    overlap_left = max(left, x_boundaries[span[0]])
                    overlap_right = min(word_right, x_boundaries[span[1]])
                    selected_indexes = [
                        index
                        for index, character_center in enumerate(character_centers)
                        if overlap_left <= character_center < overlap_right
                        or (
                            span[1] == column_count
                            and character_center == overlap_right
                        )
                    ]
                    if not selected_indexes:
                        continue
                    assigned_character_indexes.update(selected_indexes)
                    piece = "".join(characters[index] for index in selected_indexes).strip()
                    if piece:
                        first_index = selected_indexes[0]
                        last_index = selected_indexes[-1] + 1
                        segment_left = max(
                            left + width * first_index / len(characters),
                            x_boundaries[span[0]],
                        )
                        segment_right = min(
                            left + width * last_index / len(characters),
                            x_boundaries[span[1]],
                        )
                        segment_location = {
                            "left": int(round(segment_left)),
                            "top": int(round(float(location.get("top", 0)))),
                            "width": max(int(round(segment_right - segment_left)), 1),
                            "height": max(int(round(float(location.get("height", 1)))), 1),
                        }
                        cell_index = row_cell_indexes[row][span]
                        split_words.append((cell_index, piece, segment_location))

                if (
                    len(split_words) > 1
                    and assigned_character_indexes == set(range(len(characters)))
                ):
                    for cell_index, piece, segment_location in split_words:
                        cell_words[cell_index].append((piece, segment_location))
                        positioned_for_layout.append((piece, segment_location))
                    continue

            cell_words[center_cell_index].append((text, location))
            positioned_for_layout.append((text, location))
        elif center_y < table_top:
            outside_words.append((text, location))
            positioned_for_layout.append((text, location))
        elif center_y > table_bottom:
            footer_words.append((text, location))
            positioned_for_layout.append((text, location))
        else:
            positioned_for_layout.append((text, location))

    grid = [["" for _ in range(column_count)] for _ in range(row_count)]
    cells: list[TableCell] = []
    vertical_label_attempts = 0
    merged_cell_ocr_attempts = 0
    for cell_index, region in enumerate(cell_regions):
        row_start = region["row_start"]
        row_end = region["row_end"]
        col_start = region["col_start"]
        col_end = region["col_end"]
        text = _join_positioned_cell_words(cell_words[cell_index])
        restored_label = _restore_vertical_section_label(
            text,
            x_boundaries[col_start],
            y_boundaries[row_start],
            x_boundaries[col_end],
            y_boundaries[row_end],
        )
        if (
            restored_label == text
            and col_start == 0
            and col_end == 1
            and vertical_label_attempts < 4
            and (y_boundaries[row_end] - y_boundaries[row_start])
            >= (x_boundaries[col_end] - x_boundaries[col_start]) * 2.0
            and (x_boundaries[col_end] - x_boundaries[col_start])
            <= table_width * 0.25
        ):
            vertical_label_attempts += 1
            cropped_label = _recognize_vertical_section_label(
                image_path,
                x_boundaries[col_start],
                y_boundaries[row_start],
                x_boundaries[col_end],
                y_boundaries[row_end],
            )
            if cropped_label is not None:
                restored_label = cropped_label
        text = restored_label
        if (
            merged_cell_ocr_attempts < _MAX_CROPPED_MERGED_CELL_OCR_ATTEMPTS
            and row_end - row_start >= 3
            and col_start > 0
            and (x_boundaries[col_end] - x_boundaries[col_start]) >= 120
            and (y_boundaries[row_end] - y_boundaries[row_start]) >= 80
            and len("".join(clean_ocr_text(text).split())) >= 80
        ):
            merged_cell_ocr_attempts += 1
            cropped_cell = _recognize_dense_merged_table_cell(
                image_path,
                text,
                x_boundaries[col_start],
                y_boundaries[row_start],
                x_boundaries[col_end],
                y_boundaries[row_end],
            )
            if cropped_cell is not None:
                text, cropped_words = cropped_cell
                cell_words[cell_index] = cropped_words
                cell_left = x_boundaries[col_start]
                cell_top = y_boundaries[row_start]
                cell_right = x_boundaries[col_end]
                cell_bottom = y_boundaries[row_end]
                positioned_for_layout[:] = [
                    (word_text, location)
                    for word_text, location in positioned_for_layout
                    if not (
                        cell_left
                        <= float(location.get("left", 0))
                        + max(float(location.get("width", 1)), 1.0) / 2.0
                        <= cell_right
                        and cell_top
                        <= float(location.get("top", 0))
                        + max(float(location.get("height", 1)), 1.0) / 2.0
                        <= cell_bottom
                    )
                ]
                positioned_for_layout.extend(cropped_words)
        grid[row_start][col_start] = text
        cells.append(
            TableCell(
                row_start=row_start,
                row_end=row_end,
                col_start=col_start,
                col_end=col_end,
                text=text,
                left=x_boundaries[col_start],
                top=y_boundaries[row_start],
                right=x_boundaries[col_end],
                bottom=y_boundaries[row_end],
            )
        )

    paragraph_candidates = _positioned_paragraph_candidates(
        outside_words,
        image_width=image_width,
    )
    footer_candidates = [
        (text, "body", top, layout)
        for text, _style, top, layout in _positioned_paragraph_candidates(
            footer_words,
            image_width=image_width,
        )
    ]
    paragraph_candidates = sorted(
        [*paragraph_candidates, *footer_candidates],
        key=lambda candidate: candidate[2],
    )
    return OcrResult(
        image_width=image_width,
        image_height=image_height,
        paragraphs=[item[0] for item in paragraph_candidates],
        paragraph_styles=[item[1] for item in paragraph_candidates],
        paragraph_layouts=[item[3] for item in paragraph_candidates],
        positioned_words=_as_positioned_word_objects(positioned_for_layout),
        tables=[grid],
        table_layouts=[
            TableLayout(
                column_widths=[
                    max(x_boundaries[index + 1] - x_boundaries[index], 1.0)
                    for index in range(column_count)
                ],
                row_heights=[
                    max(y_boundaries[index + 1] - y_boundaries[index], 1.0)
                    for index in range(row_count)
                ],
                source_width=float(image_width),
                source_left=table_left,
                source_right=table_right,
            )
        ],
        table_cells=[cells],
    )


def _refine_table_merges_from_image_lines(
    result: OcrResult,
    image_path: Path,
    *,
    minimum_text_overlap: float = 0.85,
) -> bool:
    """Correct cloud cell merges when the source image provides a matching grid."""

    if (
        len(result.tables) != 1
        or len(result.table_cells) != 1
        or not result.positioned_words
    ):
        return False

    source_grid = result.tables[0]
    source_rows = len(source_grid)
    source_columns = max((len(row) for row in source_grid), default=0)
    if source_rows < 2 or source_columns < 2:
        return False

    positioned_words = [
        (
            word.text,
            {
                "left": int(round(word.left)),
                "top": int(round(word.top)),
                "width": max(int(round(word.width)), 1),
                "height": max(int(round(word.height)), 1),
            },
        )
        for word in result.positioned_words
        if clean_ocr_text(word.text)
    ]
    detected = _table_from_image_lines(image_path, positioned_words)
    if (
        detected is None
        or len(detected.tables) != 1
        or len(detected.table_cells) != 1
        or len(detected.tables[0]) != source_rows
        or max((len(row) for row in detected.tables[0]), default=0)
        != source_columns
    ):
        return False

    source_spans = {
        (cell.row_start, cell.row_end, cell.col_start, cell.col_end)
        for cell in result.table_cells[0]
    }
    detected_spans = {
        (cell.row_start, cell.row_end, cell.col_start, cell.col_end)
        for cell in detected.table_cells[0]
    }
    if source_spans == detected_spans:
        return False

    source_text = _compact_ocr_sequence(
        "".join(clean_ocr_text(value) for row in source_grid for value in row)
    )
    detected_text = _compact_ocr_sequence(
        "".join(clean_ocr_text(value) for row in detected.tables[0] for value in row)
    )
    if not source_text or not detected_text:
        return False
    shared_characters = sum((Counter(source_text) & Counter(detected_text)).values())
    if shared_characters / max(len(source_text), len(detected_text)) < minimum_text_overlap:
        return False

    result.tables = detected.tables
    result.table_cells = detected.table_cells
    result.table_layouts = detected.table_layouts
    result.positioned_words = detected.positioned_words
    return True


def _clean_line_boundaries(values: list[float], maximum: float) -> list[float]:
    """Sort, clamp and de-duplicate detected line centres."""

    result: list[float] = []
    for value in sorted(float(item) for item in values):
        value = min(max(value, 0.0), maximum)
        if not result or abs(value - result[-1]) >= 3.0:
            result.append(value)
    return result


def _is_low_confidence_table_rule_noise(
    text: str,
    location: dict[str, int],
) -> bool:
    """Skip short, low-confidence ASCII fragments that resemble table rules."""
    confidence = location.get("confidence")
    if confidence is None:
        return False
    token = text.strip()
    if (
        confidence < _TESSERACT_TABLE_RULE_NOISE_THRESHOLD
        and _OCR_TABLE_RULE_NOISE_RE.fullmatch(token)
    ):
        return True
    if (
        confidence < _TESSERACT_SINGLE_LETTER_NOISE_THRESHOLD
        and len(token) == 1
        and token.isascii()
        and token.isalpha()
        and location.get("height", 0) >= location.get("width", 1) * 1.2
    ):
        return True
    return False


def _keep_lines_with_table_ink(
    image_path: Path,
    x_boundaries: list[float],
    y_boundaries: list[float],
) -> tuple[list[float], list[float]]:
    """Discard long text strokes that are not full table rules.

    The projection detector can see a title's horizontal Chinese stroke as a
    short rule. A real table rule crosses a meaningful part of the table span;
    checking dark-run coverage removes those title artifacts while retaining
    partial rules in merged form cells.
    """

    if len(x_boundaries) < 2 or len(y_boundaries) < 2:
        return x_boundaries, y_boundaries
    try:
        with Image.open(image_path) as source:
            pixels = source.convert("L").load()
            image_width, image_height = source.size
            x_start = max(int(round(x_boundaries[0])), 0)
            x_end = min(int(round(x_boundaries[-1])), image_width - 1)
            y_start = max(int(round(y_boundaries[0])), 0)
            y_end = min(int(round(y_boundaries[-1])), image_height - 1)
            horizontal_span = max(x_end - x_start + 1, 1)
            vertical_span = max(y_end - y_start + 1, 1)

            def row_coverage(value: float) -> int:
                best = 0
                for row in range(max(y_start, int(round(value)) - 1), min(y_end + 1, int(round(value)) + 2)):
                    current = longest = 0
                    for column in range(x_start, x_end + 1):
                        if pixels[column, row] < 220:
                            current += 1
                            longest = max(longest, current)
                        else:
                            current = 0
                    best = max(best, longest)
                return best

            def column_coverage(value: float) -> int:
                best = 0
                for column in range(max(x_start, int(round(value)) - 1), min(x_end + 1, int(round(value)) + 2)):
                    current = longest = 0
                    for row in range(y_start, y_end + 1):
                        if pixels[column, row] < 220:
                            current += 1
                            longest = max(longest, current)
                        else:
                            current = 0
                    best = max(best, longest)
                return best

            minimum_horizontal = max(16, int(horizontal_span * 0.22))
            minimum_vertical = max(16, int(vertical_span * 0.22))
            filtered_x = [value for value in x_boundaries if column_coverage(value) >= minimum_vertical]
            filtered_y = [value for value in y_boundaries if row_coverage(value) >= minimum_horizontal]
            return (
                filtered_x if len(filtered_x) >= 2 else x_boundaries,
                filtered_y if len(filtered_y) >= 2 else y_boundaries,
            )
    except (OSError, ValueError):
        return x_boundaries, y_boundaries


def _trim_empty_short_table_edges(
    x_boundaries: list[float],
    y_boundaries: list[float],
    positioned_words: list[tuple[str, dict[str, int]]],
) -> list[float]:
    """Remove tiny empty edge strips caused by nearby form-field underlines."""

    result = list(y_boundaries)

    def contains_text(top: float, bottom: float) -> bool:
        return any(
            x_boundaries[0] <= float(location.get("left", 0))
            + max(float(location.get("width", 1)), 1.0) / 2.0
            <= x_boundaries[-1]
            and top
            <= float(location.get("top", 0))
            + max(float(location.get("height", 1)), 1.0) / 2.0
            <= bottom
            for _text, location in positioned_words
        )

    while len(result) > 2:
        edge_height = result[1] - result[0]
        next_height = result[2] - result[1]
        if (
            edge_height > 0
            and edge_height <= 24
            and edge_height < next_height * 0.35
            and not contains_text(result[0], result[1])
        ):
            result.pop(0)
        else:
            break

    while len(result) > 2:
        previous_height = result[-2] - result[-3]
        edge_height = result[-1] - result[-2]
        if (
            edge_height > 0
            and edge_height <= 24
            and edge_height < previous_height * 0.35
            and not contains_text(result[-2], result[-1])
        ):
            result.pop()
        else:
            break

    return result


def _table_rule_presence(
    image_path: Path,
    x_boundaries: list[float],
    y_boundaries: list[float],
) -> tuple[list[list[bool]], list[list[bool]]]:
    """Measure each grid edge separately so partial rules preserve cell merges."""

    row_count = max(len(y_boundaries) - 1, 0)
    column_count = max(len(x_boundaries) - 1, 0)
    vertical_rules = [[True] * (column_count + 1) for _ in range(row_count)]
    horizontal_rules = [[True] * column_count for _ in range(row_count + 1)]
    try:
        with Image.open(image_path) as source:
            gray = source.convert("L")
            try:
                pixels = gray.load()
                image_width, image_height = gray.size

                def coverage(
                    position: float,
                    start: float,
                    end: float,
                    *,
                    vertical: bool,
                ) -> float:
                    start_pixel = int(round(start))
                    end_pixel = int(round(end))
                    if end_pixel - start_pixel >= 8:
                        start_pixel += 2
                        end_pixel -= 2
                    coordinate = int(round(position))
                    if vertical:
                        axis_start = max(start_pixel, 0)
                        axis_end = min(end_pixel, image_height)
                        sample_coordinates = range(
                            max(coordinate - 2, 0),
                            min(coordinate + 3, image_width),
                        )
                        total = max(axis_end - axis_start, 0)
                        dark = sum(
                            any(pixels[x, y] < 220 for x in sample_coordinates)
                            for y in range(axis_start, axis_end)
                        )
                    else:
                        axis_start = max(start_pixel, 0)
                        axis_end = min(end_pixel, image_width)
                        sample_coordinates = range(
                            max(coordinate - 2, 0),
                            min(coordinate + 3, image_height),
                        )
                        total = max(axis_end - axis_start, 0)
                        dark = sum(
                            any(pixels[x, y] < 220 for y in sample_coordinates)
                            for x in range(axis_start, axis_end)
                        )
                    return dark / total if total else 0.0

                for row in range(row_count):
                    for boundary in range(1, column_count):
                        vertical_rules[row][boundary] = (
                            coverage(
                                x_boundaries[boundary],
                                y_boundaries[row],
                                y_boundaries[row + 1],
                                vertical=True,
                            )
                            >= 0.75
                        )
                for boundary in range(1, row_count):
                    for column in range(column_count):
                        horizontal_rules[boundary][column] = (
                            coverage(
                                y_boundaries[boundary],
                                x_boundaries[column],
                                x_boundaries[column + 1],
                                vertical=False,
                            )
                            >= 0.75
                        )
            finally:
                gray.close()
    except (OSError, ValueError):
        # If pixel inspection fails, preserve the independent grid cells rather
        # than guessing that missing data means a merged cell.
        return vertical_rules, horizontal_rules
    return vertical_rules, horizontal_rules


def _restore_vertical_section_label(
    text: str,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> str:
    """Restore missed glyphs in the two known tall form-section labels."""

    width = right - left
    if width <= 0 or bottom - top < width * 2.0:
        return text
    observed = "".join(clean_ocr_text(text).split())
    collapsed_repeats = re.sub(r"(.)\1+", r"\1", observed)
    for expected in ("学生基本情况", "家庭基本情况"):
        if (
            collapsed_repeats == expected
            or (
                len(observed) > len(expected)
                and len(observed) % len(expected) == 0
                and observed == expected * (len(observed) // len(expected))
            )
        ):
            return "\n".join(expected)
        if (
            not observed.startswith(expected[:2])
            or len(observed) < len(expected) - 2
            or len(observed) > len(expected)
        ):
            continue
        cursor = 0
        for character in observed:
            cursor = expected.find(character, cursor)
            if cursor < 0:
                break
            cursor += 1
        else:
            return "\n".join(expected)
    return text


def _recognize_vertical_section_label(
    image_path: Path,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> str | None:
    """Re-read a tall first-column label in isolation when page OCR misses it."""
    width = right - left
    height = bottom - top
    if width <= 8 or height < width * 2.0:
        return None

    try:
        with Image.open(image_path) as source:
            crop_box = (
                max(0, int(floor(left)) + 2),
                max(0, int(floor(top)) + 2),
                min(source.width, int(ceil(right)) - 2),
                min(source.height, int(ceil(bottom)) - 2),
            )
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                return None
            crop = source.crop(crop_box)
            try:
                normalized_crop = crop.convert("RGB")
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="iscan-vertical-label-"
                    ) as temporary_dir:
                        crop_path = Path(temporary_dir) / "label.png"
                        normalized_crop.save(crop_path, format="PNG")
                        confidences: list[tuple[float, str]] = []
                        words = _tesseract_positioned_words(
                            crop_path,
                            page_segmentation_mode=6,
                            timeout_seconds=5,
                            word_confidences=confidences,
                        )
                finally:
                    normalized_crop.close()
            finally:
                crop.close()
    except (OSError, ValueError):
        return None

    if not words or not confidences:
        return None
    mean_confidence = sum(score for score, _text in confidences) / len(confidences)
    if mean_confidence < _TESSERACT_LOW_CONFIDENCE_THRESHOLD:
        return None
    observed = "".join(
        "".join(clean_plain_ocr_text(text).split())
        for text, _location in words
    )
    return _restore_vertical_section_label(observed, 0, 0, width - 4, height - 4)


def _recognize_dense_merged_table_cell(
    image_path: Path,
    existing_text: str,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> tuple[str, list[tuple[str, dict[str, int]]]] | None:
    """Re-read a dense merged table cell, keeping its OCR boxes in page coordinates."""
    width = right - left
    height = bottom - top
    source_text = clean_ocr_text(existing_text)
    source_compact = "".join(source_text.split())
    if width < 120 or height < 80 or len(source_compact) < 80:
        return None

    try:
        with Image.open(image_path) as source:
            crop_box = (
                max(0, int(floor(left)) + 2),
                max(0, int(floor(top)) + 2),
                min(source.width, int(ceil(right)) - 2),
                min(source.height, int(ceil(bottom)) - 2),
            )
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                return None
            crop = source.crop(crop_box)
            try:
                normalized_crop = crop.convert("RGB")
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="iscan-merged-cell-"
                    ) as temporary_dir:
                        crop_path = Path(temporary_dir) / "cell.png"
                        normalized_crop.save(crop_path, format="PNG")
                        confidences: list[tuple[float, str]] = []
                        words = _tesseract_positioned_words(
                            crop_path,
                            page_segmentation_mode=6,
                            timeout_seconds=5,
                            word_confidences=confidences,
                        )
                finally:
                    normalized_crop.close()
            finally:
                crop.close()
    except (OSError, ValueError):
        return None

    if len(words) < 3 or not confidences:
        return None
    mean_confidence = sum(score for score, _text in confidences) / len(confidences)
    if mean_confidence < _TESSERACT_LOW_CONFIDENCE_THRESHOLD:
        return None

    candidate_text = _join_positioned_cell_words(words)
    candidate_compact = "".join(clean_ocr_text(candidate_text).split())
    source_chinese_characters = sum(
        "\u3400" <= character <= "\u9fff" for character in source_compact
    )
    candidate_chinese_characters = sum(
        "\u3400" <= character <= "\u9fff" for character in candidate_compact
    )
    if (
        len(candidate_compact) < max(50, int(len(source_compact) * 0.75))
        or len(candidate_compact) > max(
            int(len(source_compact) * 1.8),
            len(source_compact) + 64,
        )
        or candidate_chinese_characters < source_chinese_characters
        or candidate_compact.count("□") < source_compact.count("□")
        or (
            candidate_chinese_characters == source_chinese_characters
            and candidate_compact.count("□") == source_compact.count("□")
        )
    ):
        return None

    crop_left, crop_top = crop_box[0], crop_box[1]
    positioned_words = [
        (
            text,
            {
                "left": crop_left + int(location.get("left", 0)),
                "top": crop_top + int(location.get("top", 0)),
                "width": max(int(location.get("width", 1)), 1),
                "height": max(int(location.get("height", 1)), 1),
            },
        )
        for text, location in words
    ]
    return candidate_text, positioned_words


def _join_positioned_cell_words(
    words: list[tuple[str, dict[str, int]]],
) -> str:
    """Join general-OCR words into lines while preserving cell line breaks."""

    if not words:
        return ""
    ordered = sorted(
        words,
        key=lambda item: (float(item[1].get("top", 0)), float(item[1].get("left", 0))),
    )
    lines: list[list[tuple[str, dict[str, int]]]] = []
    line_tops: list[float] = []
    line_tops_are_ordered = True
    for word in ordered:
        top = float(word[1].get("top", 0))
        height = max(float(word[1].get("height", 1)), 1.0)
        tolerance = max(height * 0.65, 3.0)
        line_index = _first_line_within_vertical_tolerance(
            line_tops,
            top,
            tolerance,
            line_tops_are_ordered=line_tops_are_ordered,
        )
        if line_index is None:
            if not isfinite(top) or (line_tops and top < line_tops[-1]):
                line_tops_are_ordered = False
            line_tops.append(top)
            lines.append([word])
        else:
            lines[line_index].append(word)
    ordered_lines = sorted(zip(line_tops, lines), key=lambda item: item[0])
    return clean_ocr_text(
        "\n".join(
            _join_ocr_lines(
                [text for text, _ in sorted(line, key=lambda item: item[1].get("left", 0))]
            )
            for _, line in ordered_lines
        ).strip()
    )


def _paragraphs_from_positioned_words(
    positioned_words: list[tuple[str, dict[str, int]]],
) -> list[str]:
    return [candidate[0] for candidate in _positioned_paragraph_candidates(positioned_words)]


def _first_line_within_vertical_tolerance(
    line_tops: list[float],
    top: float,
    tolerance: float,
    *,
    line_tops_are_ordered: bool = True,
) -> int | None:
    """Find the first legacy line match without scanning every prior line."""

    if (
        line_tops_are_ordered
        and isfinite(top)
        and isfinite(tolerance)
    ):
        first_possible = bisect_left(line_tops, top - tolerance)
        last_possible_top = top + tolerance
        for index in range(first_possible, len(line_tops)):
            line_top = line_tops[index]
            if line_top > last_possible_top:
                break
            if abs(top - line_top) <= tolerance:
                return index
        return None

    # Keep the exact behavior for malformed/non-finite coordinates or inputs
    # whose sorting order is not numeric (for example numeric strings).
    return next(
        (
            index
            for index, line_top in enumerate(line_tops)
            if abs(top - line_top) <= tolerance
        ),
        None,
    )


def _positioned_paragraph_candidates(
    positioned_words: list[tuple[str, dict[str, int]]],
    image_width: float = 0.0,
    *,
    preserve_content: bool = False,
) -> list[tuple[str, str, float, ParagraphLayout]]:
    """Group coordinate OCR into lines and classify visual heading levels.

    The table OCR endpoint already supplies header metadata, but the general
    OCR endpoint only returns word boxes. Keeping this geometry pass shared
    means a plain text scan and a table-service fallback preserve the visible
    title/subtitle instead of flattening every line into body text.
    """

    if not positioned_words:
        return []
    clean_text = clean_plain_ocr_text if preserve_content else clean_ocr_text
    ordered = sorted(
        positioned_words,
        key=lambda item: (item[1].get("top", 0), item[1].get("left", 0)),
    )
    lines: list[list[tuple[str, dict[str, int]]]] = []
    line_tops: list[float] = []
    line_tops_are_ordered = True
    for word in ordered:
        top = float(word[1].get("top", 0))
        height = max(float(word[1].get("height", 1)), 1.0)
        tolerance = max(height * 0.65, 3.0)
        line_index = _first_line_within_vertical_tolerance(
            line_tops,
            top,
            tolerance,
            line_tops_are_ordered=line_tops_are_ordered,
        )
        if line_index is None:
            if not isfinite(top) or (line_tops and top < line_tops[-1]):
                line_tops_are_ordered = False
            line_tops.append(top)
            lines.append([word])
        else:
            lines[line_index].append(word)

    line_values: list[tuple[str, float, float, float, float, ParagraphLayout]] = []
    for line in sorted(zip(line_tops, lines), key=lambda item: item[0]):
        line_words = sorted(line[1], key=lambda item: item[1].get("left", 0))
        cleaned_words = [clean_text(word) for word, _ in line_words]
        text = _join_positioned_ocr_line(
            list(zip(cleaned_words, [location for _, location in line_words])),
            preserve_content=preserve_content,
        )
        if not text:
            continue
        left = min(float(location.get("left", 0)) for _, location in line_words)
        right = max(
            float(location.get("left", 0)) + float(location.get("width", 0))
            for _, location in line_words
        )
        top = min(float(location.get("top", 0)) for _, location in line_words)
        bottom = max(
            float(location.get("top", 0)) + float(location.get("height", 1))
            for _, location in line_words
        )
        line_height = max(bottom - top, 1.0)
        line_values.append(
            (
                text,
                left,
                right,
                top,
                line_height,
                ParagraphLayout(
                    fragments=[
                        TextFragment(
                            text=cleaned,
                            left=float(location.get("left", 0)),
                            top=float(location.get("top", 0)),
                            width=max(float(location.get("width", 0)), 1.0),
                            height=max(float(location.get("height", 1)), 1.0),
                        )
                        for cleaned, (_, location) in zip(cleaned_words, line_words)
                        if cleaned
                    ],
                    source_width=float(image_width),
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                ),
            )
        )

    if not line_values:
        return []
    effective_width = image_width or max(item[2] for item in line_values)
    maximum_height = max(item[4] for item in line_values)
    first_top = line_values[0][3]
    title_seen = False
    subtitle_seen = False
    result: list[tuple[str, str, float, ParagraphLayout]] = []
    for text, left, right, top, height, layout in line_values:
        centered = abs((left + right) / 2 - effective_width / 2) <= max(
            effective_width * 0.14,
            maximum_height * 2.0,
        )
        prominent = height >= maximum_height * 0.78
        near_top = top <= first_top + max(maximum_height * 2.5, 14.0)
        if centered and not title_seen and (prominent or near_top):
            style = "title"
            title_seen = True
        elif (
            centered
            and title_seen
            and not subtitle_seen
            and top <= first_top + max(maximum_height * 5.0, 32.0)
            and (prominent or len(text) <= 24)
        ):
            style = "subtitle"
            subtitle_seen = True
        else:
            style = "body"
        result.append((text, style, top, layout))
    return result


def _reflow_prose_candidates(
    candidates: list[tuple[str, str, float, ParagraphLayout]],
    image_width: float = 0.0,
) -> tuple[list[str], list[str]]:
    """Join wrapped OCR lines into readable prose paragraphs.

    Text scans do not need their source line breaks reproduced. Keep visual
    headings separate, and use larger vertical gaps or a first-line indent as
    paragraph boundaries. The resulting paragraphs can then reflow in the
    editor and in Word at the reader's page width.
    """

    body_items = [item for item in candidates if item[1] == "body"]
    heights = [
        max(item[3].bottom - item[3].top, 1.0)
        for item in body_items
        if item[3] is not None
    ]
    typical_height = median(heights) if heights else 1.0
    gaps = [
        max(0.0, current[3].top - previous[3].bottom)
        for previous, current in zip(body_items, body_items[1:])
        if previous[3] is not None and current[3] is not None
    ]
    compact_gaps = [gap for gap in gaps if gap <= typical_height * 0.8]
    gap_threshold = (
        max(median(compact_gaps) * 2.0, typical_height * 0.85, 2.0)
        if compact_gaps
        else typical_height * 1.2
    )
    body_lefts = [item[3].left for item in body_items if item[3] is not None]
    base_left = min(body_lefts, default=0.0)
    indent_threshold = max(typical_height * 1.1, image_width * 0.012, 8.0)

    paragraphs: list[str] = []
    styles: list[str] = []
    body_lines: list[str] = []
    previous_body: tuple[str, str, float, ParagraphLayout] | None = None

    def flush_body() -> None:
        nonlocal body_lines
        text = _join_ocr_lines(body_lines, preserve_content=True)
        if text:
            paragraphs.append(text)
            styles.append("body")
        body_lines = []

    for candidate in candidates:
        text, style, _, layout = candidate
        if style != "body":
            flush_body()
            paragraphs.append(text)
            styles.append(style)
            previous_body = None
            continue

        if body_lines and previous_body is not None:
            previous_layout = previous_body[3]
            gap = max(0.0, layout.top - previous_layout.bottom)
            starts_indented = layout.left > base_left + indent_threshold
            if gap > gap_threshold or starts_indented:
                flush_body()
        body_lines.append(text)
        previous_body = candidate

    flush_body()
    return paragraphs, styles


def reflow_plain_text_result(result: OcrResult) -> bool:
    """Drop scan coordinates and reflow a text-only OCR result as prose.

    Coordinate geometry remains essential for detected tables. For ordinary
    text, matching one-line OCR paragraphs are joined into reading paragraphs;
    provider paragraphs that already contain complete prose are preserved.
    """

    if result.tables:
        return False

    changed = bool(result.positioned_words or result.paragraph_layouts)
    if result.positioned_words:
        positioned_words = [
            (
                word.text,
                {
                    "left": int(round(word.left)),
                    "top": int(round(word.top)),
                    "width": max(int(round(word.width)), 1),
                    "height": max(int(round(word.height)), 1),
                },
            )
            for word in result.positioned_words
        ]
        candidates = _positioned_paragraph_candidates(
            positioned_words,
            image_width=result.image_width,
            preserve_content=True,
        )
        is_linewise_result = (
            len(candidates) == len(result.paragraphs)
            and bool(candidates)
            and all(
                _same_ocr_text(paragraph, candidate[0])
                for paragraph, candidate in zip(result.paragraphs, candidates)
            )
        )
        if not result.paragraphs and candidates:
            is_linewise_result = True
        if is_linewise_result:
            result.paragraphs, result.paragraph_styles = _reflow_prose_candidates(
                candidates,
                image_width=result.image_width,
            )

    result.paragraph_layouts = []
    result.positioned_words = []
    if len(result.paragraph_styles) < len(result.paragraphs):
        result.paragraph_styles.extend(
            ["body"] * (len(result.paragraphs) - len(result.paragraph_styles))
        )
    return changed


def _paragraphs_from_payload(payload: dict[str, object]) -> list[str]:
    """Build real paragraphs instead of exporting every wrapped OCR line."""

    words: list[str] = []
    for item in payload.get("words_result", []):
        value = item.get("words") if isinstance(item, dict) else item
        cleaned = clean_plain_ocr_text(value) if value else ""
        if cleaned:
            words.append(cleaned)

    paragraphs: list[str] = []
    paragraph_groups = payload.get("paragraphs_result", [])
    if not isinstance(paragraph_groups, list):
        # Compatible OCR providers may return null when paragraph grouping is
        # unavailable. Keep the recognized text and treat it as one prose block.
        paragraph_groups = []
    if isinstance(paragraph_groups, list):
        group_for_word: dict[int, int] = {}
        group_first_indices: dict[int, int] = {}
        for group_number, group in enumerate(paragraph_groups):
            if not isinstance(group, dict):
                continue
            indices = group.get("words_result_idx", [])
            if not isinstance(indices, list):
                continue
            for value in indices:
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(words):
                    group_for_word.setdefault(index, group_number)
                    group_first_indices[group_number] = min(
                        index,
                        group_first_indices.get(group_number, index),
                    )

        # OCR providers may return paragraph groups out of reading order and
        # may leave some recognized lines ungrouped. Rebuild the paragraphs
        # by the original words_result order so those lines stay in place.
        ordered_groups = sorted(group_first_indices, key=group_first_indices.get)
        ordered_group_number = {
            group_number: order
            for order, group_number in enumerate(ordered_groups)
        }
        word_groups: list[int | None] = [
            ordered_group_number.get(group_for_word.get(index))
            for index in range(len(words))
        ]
        previous_groups: list[int | None] = []
        previous_group: int | None = None
        for group_number in word_groups:
            previous_groups.append(previous_group)
            if group_number is not None:
                previous_group = group_number
        following_groups: list[int | None] = [None] * len(word_groups)
        following_group: int | None = None
        for index in range(len(word_groups) - 1, -1, -1):
            following_groups[index] = following_group
            if word_groups[index] is not None:
                following_group = word_groups[index]
        for index, group_number in enumerate(word_groups):
            if group_number is not None:
                continue
            previous = previous_groups[index]
            following = following_groups[index]
            word_groups[index] = previous if previous is not None else following

        current_group: int | None = None
        current_lines: list[str] = []
        for index, word in enumerate(words):
            group_number = word_groups[index]
            if current_lines and group_number != current_group:
                joined = _join_ocr_lines(current_lines, preserve_content=True)
                if joined:
                    paragraphs.append(joined)
                current_lines = []
            current_group = group_number
            current_lines.append(word)
        if current_lines:
            joined = _join_ocr_lines(current_lines, preserve_content=True)
            if joined:
                paragraphs.append(joined)

    # Some compatible OCR services use `results` instead of words_result.
    results = payload.get("results", [])
    # `results` can contain the same text in a different block order. When the
    # provider supplied its canonical words_result sequence, do not append an
    # alternate representation at the end of an otherwise complete document.
    if not words and isinstance(results, list) and results:
        (
            candidate_lengths,
            candidates_by_length,
            matcher_cache,
        ) = _index_comparable_texts(paragraphs)
        for item in results:
            if not isinstance(item, dict):
                continue
            value = item.get("words") or item.get("text") or item.get("content")
            cleaned = clean_plain_ocr_text(value) if value else ""
            if cleaned and _matching_comparable_text_index(
                cleaned,
                candidate_lengths,
                candidates_by_length,
                matcher_cache,
            ) is None:
                _append_indexed_text(
                    paragraphs,
                    cleaned,
                    candidate_lengths,
                    candidates_by_length,
                )
    return paragraphs


def _plain_text_confidence(payload: dict[str, object]) -> tuple[float, float] | None:
    """Return character-weighted confidence and the weakest reported token."""

    weighted_average_total = 0.0
    total_character_weight = 0
    weakest_values: list[float] = []

    def probability_value(value: object) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if 0.0 <= parsed <= 1.0 else None

    for item in payload.get("words_result", []):
        if not isinstance(item, dict):
            continue
        probability = item.get("probability")
        if isinstance(probability, dict):
            average = probability_value(probability.get("average"))
            weakest = probability_value(probability.get("min"))
        else:
            average = probability_value(probability)
            weakest = average
        if average is None:
            average = weakest
        if average is None:
            continue
        recognized_text = item.get("words")
        character_weight = (
            max(len(recognized_text.strip()), 1)
            if isinstance(recognized_text, str)
            else 1
        )
        weighted_average_total += average * character_weight
        total_character_weight += character_weight
        weakest_values.append(weakest if weakest is not None else average)
    if not total_character_weight:
        return None
    return weighted_average_total / total_character_weight, min(weakest_values)


def _confidence_is_low(confidence: tuple[float, float] | None) -> bool:
    return confidence is not None and (confidence[0] < 0.92 or confidence[1] < 0.82)


def _plain_text_confidence_is_low(payload: dict[str, object]) -> bool:
    return _confidence_is_low(_plain_text_confidence(payload))


def _low_confidence_text_examples(
    payload: dict[str, object],
    *,
    limit: int = 5,
) -> list[str]:
    """Return a few OCR tokens that should be checked against the source image."""

    if limit <= 0:
        return []
    examples: list[tuple[float, int, str]] = []

    def probability_value(
        value: object,
        *,
        allow_percent: bool = False,
    ) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if allow_percent and 1.0 < parsed <= 100.0:
            parsed /= 100.0
        return parsed if 0.0 <= parsed <= 1.0 else None

    word_lists: list[list[object]] = []
    for key in ("words_result", "positioned_words", "words", "results"):
        raw_words = payload.get(key)
        if isinstance(raw_words, list):
            word_lists.append(raw_words)
    word_count = 0
    for words_result in word_lists:
        for index, item in enumerate(words_result):
            if not isinstance(item, dict):
                continue
            recognized = (
                item.get("words")
                or item.get("text")
                or item.get("content")
                or item.get("value")
            )
            text = clean_plain_ocr_text(recognized) if recognized else ""
            if not text:
                continue
            probability = item.get("probability")
            allow_percent = False
            if probability is None:
                probability = item.get("confidence")
                if probability is None:
                    probability = item.get("score")
                allow_percent = probability is not None
            if isinstance(probability, dict):
                average_value = probability.get("average")
                if average_value is None:
                    average_value = probability.get("mean")
                weakest_value = probability.get("min")
                if weakest_value is None:
                    weakest_value = probability.get("minimum")
                average = probability_value(
                    average_value,
                    allow_percent=allow_percent,
                )
                weakest = probability_value(
                    weakest_value,
                    allow_percent=allow_percent,
                )
            else:
                average = probability_value(
                    probability,
                    allow_percent=allow_percent,
                )
                weakest = average
            if average is None:
                average = weakest
            if average is None or (
                average >= 0.92 and (weakest is None or weakest >= 0.82)
            ):
                continue
            score = min(average, weakest if weakest is not None else average)
            examples.append((score, word_count + index, text))
        word_count += len(words_result)

    supplied = payload.get("low_confidence_examples", payload.get("low_confidence_words"))
    if isinstance(supplied, list):
        for index, item in enumerate(supplied):
            raw_text = (
                item.get("text") or item.get("words")
                if isinstance(item, dict)
                else item
            )
            text = clean_plain_ocr_text(raw_text) if raw_text else ""
            if text:
                examples.append((0.0, word_count + index, text))

    selected: list[str] = []
    seen: set[str] = set()
    for _score, _index, text in sorted(examples, key=lambda item: (item[0], item[1])):
        comparable = _comparable_ocr_text(text)
        if not comparable or comparable in seen:
            continue
        seen.add(comparable)
        selected.append(text if len(text) <= 24 else f"{text[:23]}…")
        if len(selected) >= max(limit, 0):
            break
    return selected


def _merge_confidence_examples(*groups: list[str], limit: int = 5) -> list[str]:
    if limit <= 0:
        return []
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            text = clean_plain_ocr_text(value)
            comparable = _comparable_ocr_text(text)
            if not text or not comparable or comparable in seen:
                continue
            seen.add(comparable)
            merged.append(text if len(text) <= 24 else f"{text[:23]}…")
            if len(merged) >= max(limit, 0):
                return merged
    return merged


def _plain_text_similarity(first: list[str], second: list[str]) -> float:
    """Compare prose candidates while ignoring OCR line and paragraph breaks."""
    first_text = re.sub(
        r"\s+",
        "",
        "".join(clean_plain_ocr_text(value) for value in first),
    )
    second_text = re.sub(
        r"\s+",
        "",
        "".join(clean_plain_ocr_text(value) for value in second),
    )
    if not first_text or not second_text:
        return 0.0
    if first_text == second_text:
        return 1.0
    matcher = SequenceMatcher(
        None,
        first_text,
        second_text,
        autojunk=max(len(first_text), len(second_text)) > 8192,
    )
    return matcher.ratio()


def _prefer_plain_text_ocr_candidate(
    primary_paragraphs: list[str],
    primary_confidence: tuple[float, float] | None,
    alternate_paragraphs: list[str],
    alternate_confidence: tuple[float, float] | None,
    *,
    reference_paragraphs: list[str] | None = None,
) -> bool:
    """Prefer a clearer prose retry without requiring near-identical wording."""

    primary_text = re.sub(r"\s+", "", "".join(primary_paragraphs))
    alternate_text = re.sub(r"\s+", "", "".join(alternate_paragraphs))
    primary_length = len(primary_text)
    alternate_length = len(alternate_text)
    # Text-only OCR should prioritize readable, accurate prose over matching
    # every character from the first pass. Still reject a candidate that is
    # much shorter, since it may have dropped a complete line or paragraph.
    if not primary_length or alternate_length < (90 * primary_length + 99) // 100:
        return False

    # Ignore layout whitespace and accept meaningful wording corrections when
    # the independent coordinate pass supports them. Otherwise require the
    # normal confidence improvement and page-text overlap. For long pages,
    # autojunk reduces work on very frequent characters.
    primary_similarity = _plain_text_similarity(primary_paragraphs, alternate_paragraphs)
    reference_supports_alternate = False
    if reference_paragraphs:
        primary_reference_similarity = _plain_text_similarity(
            primary_paragraphs,
            reference_paragraphs,
        )
        alternate_reference_similarity = _plain_text_similarity(
            alternate_paragraphs,
            reference_paragraphs,
        )
        reference_supports_alternate = (
            alternate_reference_similarity >= 0.78
            and alternate_reference_similarity >= primary_reference_similarity + 0.04
        )
    if primary_similarity < 0.78 and not reference_supports_alternate:
        return False

    if primary_confidence is None or alternate_confidence is None:
        return reference_supports_alternate

    primary_score = primary_confidence[0] * 0.6 + primary_confidence[1] * 0.4
    alternate_score = alternate_confidence[0] * 0.6 + alternate_confidence[1] * 0.4
    if reference_supports_alternate and alternate_score >= primary_score - 0.02:
        return True
    return alternate_score >= primary_score + 0.04


def _join_ocr_lines(lines: list[str], *, preserve_content: bool = False) -> str:
    clean_text = clean_plain_ocr_text if preserve_content else clean_ocr_text
    return _join_clean_ocr_lines(
        (clean_text(line) for line in lines),
        preserve_content=preserve_content,
    )


def _join_clean_ocr_lines(
    cleaned_lines: Iterator[str],
    *,
    preserve_content: bool = False,
) -> str:
    """Join recognized fragments while keeping prose and form cleanup separate."""

    result = ""
    for cleaned in cleaned_lines:
        if not cleaned:
            continue
        if result and cleaned[0].isascii() and cleaned[0].isalnum():
            previous_character = result[-1]
            if previous_character.isascii() and previous_character.isalnum():
                result += " "
            elif previous_character in ",.;:!?":
                # A wrapped English line can end with punctuation; keep the
                # following word separate without splitting decimal numbers.
                previous_word_character = result[-2] if len(result) > 1 else ""
                continues_number = (
                    previous_character in ",.:"
                    and previous_word_character.isdigit()
                    and cleaned[0].isdigit()
                )
                if not continues_number:
                    result += " "
        result += cleaned
    # 术语可能由多个 OCR 块拼接而成；拼接后再清理一次，才能处理
    # “比” + “Top-K” + “更自然”或“达到” + “p 的”等边界。
    clean_text = clean_plain_ocr_text if preserve_content else clean_ocr_text
    return clean_text(result.strip())


def _join_positioned_ocr_line(
    words: list[tuple[str, dict[str, float | int]]],
    *,
    preserve_content: bool = False,
) -> str:
    """Join OCR fragments, using box gaps to distinguish letters from words."""
    clean_text = clean_plain_ocr_text if preserve_content else clean_ocr_text
    result = ""
    previous_text = ""
    previous_location: dict[str, float | int] | None = None
    for raw_text, location in words:
        text = clean_text(raw_text)
        if not text:
            continue
        if result:
            previous_character = result[-1]
            current_character = text[0]
            ascii_pair = (
                previous_character.isascii()
                and previous_character.isalnum()
                and current_character.isascii()
                and current_character.isalnum()
            )
            character_boxes_are_contiguous = False
            if (
                ascii_pair
                and len(previous_text) == 1
                and len(text) == 1
                and previous_location is not None
            ):
                try:
                    previous_width = float(previous_location.get("width", 0))
                    previous_height = max(float(previous_location.get("height", 1)), 1.0)
                    current_left = float(location.get("left", 0))
                    current_width = float(location.get("width", 0))
                    current_height = max(float(location.get("height", 1)), 1.0)
                    gap = current_left - (
                        float(previous_location.get("left", 0)) + previous_width
                    )
                    typical_height = min(previous_height, current_height)
                    character_boxes_are_contiguous = (
                        previous_width > 0
                        and current_width > 0
                        and previous_width <= typical_height * 1.4
                        and current_width <= typical_height * 1.4
                        and gap <= max(typical_height * 0.16, 1.0)
                    )
                except (TypeError, ValueError, OverflowError):
                    character_boxes_are_contiguous = False
            if ascii_pair and not character_boxes_are_contiguous:
                result += " "
            elif (
                current_character.isascii()
                and current_character.isalnum()
                and previous_character in ",.;:!?"
            ):
                previous_word_character = result[-2] if len(result) > 1 else ""
                continues_number = (
                    previous_character in ",.:"
                    and previous_word_character.isdigit()
                    and current_character.isdigit()
                )
                if not continues_number:
                    result += " "
        result += text
        previous_text = text
        previous_location = location
    return clean_text(result.strip())


def _paragraph_blocks_outside_tables(
    tables: list[list[TableCell]],
    positioned_words: list[tuple[str, dict[str, int]]],
    image_width: float,
) -> list[tuple[str, str, float, ParagraphLayout]]:
    """Recover text outside all detected grids without duplicating cell text."""

    cell_bounds = [
        (cell.left, cell.top, cell.right, cell.bottom)
        for cells in tables
        for cell in cells
        if cell.right > cell.left and cell.bottom > cell.top
    ]
    if not cell_bounds:
        return []
    first_table_top = min(bounds[1] for bounds in cell_bounds)
    outside_words: list[tuple[str, dict[str, int]]] = []
    for text, location in positioned_words:
        left = float(location.get("left", 0))
        top = float(location.get("top", 0))
        width = max(float(location.get("width", 1)), 1.0)
        height = max(float(location.get("height", 1)), 1.0)
        center_x = left + width / 2
        center_y = top + height / 2
        if any(
            cell_left <= center_x <= cell_right
            and cell_top <= center_y <= cell_bottom
            for cell_left, cell_top, cell_right, cell_bottom in cell_bounds
        ):
            continue
        outside_words.append((text, location))

    return [
        (
            text,
            style if layout.top < first_table_top else "body",
            top,
            layout,
        )
        for text, style, top, layout in _positioned_paragraph_candidates(
            outside_words, image_width=image_width
        )
    ]


def _merge_paragraph_candidates(
    preferred: list[tuple[str, str, float, ParagraphLayout | None]],
    additional: list[tuple[str, str, float, ParagraphLayout | None]],
) -> list[tuple[str, str, float, ParagraphLayout | None]]:
    merged: list[tuple[str, str, float, ParagraphLayout | None]] = []
    comparable_lengths: list[int] = []
    comparable_by_length: dict[int, list[tuple[int, str]]] = {}
    matcher_cache: OrderedDict[int, SequenceMatcher] = OrderedDict()
    exact_comparables: set[str] = set()

    def append_if_unique(
        candidate: tuple[str, str, float, ParagraphLayout | None],
    ) -> None:
        comparable = _comparable_ocr_text(candidate[0])
        if comparable:
            if comparable in exact_comparables:
                return
            if _matching_indexed_text(
                comparable,
                comparable_lengths,
                comparable_by_length,
                matcher_cache,
            ) is not None:
                return
            _append_comparable_index(
                len(merged),
                comparable,
                comparable_lengths,
                comparable_by_length,
            )
            exact_comparables.add(comparable)
        merged.append(candidate)

    for candidate in sorted(preferred, key=lambda item: item[2]):
        append_if_unique(candidate)
    for candidate in sorted(additional, key=lambda item: item[2]):
        append_if_unique(candidate)
    ordered = sorted(merged, key=lambda item: item[2])
    # Table OCR may return isolated labels (for example “年级”“班别”) that
    # positioned OCR has already captured in their complete source line.
    # Remove only these short contained duplicates; never discard full text.
    compact_texts = [re.sub(r"\s+", "", candidate[0]) for candidate in ordered]
    short_fragments = {text for text in compact_texts if 0 < len(text) <= 4}
    contained_fragments: set[str] = set()
    if short_fragments:
        maximum_fragment_length = max(map(len, short_fragments))
        for text in compact_texts:
            for fragment_length in range(1, min(maximum_fragment_length, len(text) - 1) + 1):
                contained_fragments.update(
                    text[start:start + fragment_length]
                    for start in range(len(text) - fragment_length + 1)
                )
    ordered = [
        candidate
        for candidate, compact in zip(ordered, compact_texts)
        if not (0 < len(compact) <= 4 and compact in contained_fragments)
    ]
    title_candidates = [
        (index, candidate)
        for index, candidate in enumerate(ordered)
        if candidate[1] == "title"
    ]
    title_index = None
    if title_candidates:
        title_index = max(
            title_candidates,
            key=lambda item: (
                40 if any(marker in item[1][0] for marker in ("申请表", "大赛", "比赛", "结果")) else 0,
                len(item[1][0]),
                -item[1][2],
            ),
        )[0]

    headings: list[tuple[str, str, float, ParagraphLayout | None]] = []
    body: list[tuple[str, str, float, ParagraphLayout | None]] = []
    for index, (text, style, top, layout) in enumerate(ordered):
        if index == title_index:
            headings.append((text, "title", top, layout))
        elif style == "subtitle" and (
            len(text) >= 5 or text.startswith(("(", "（")) or "版" in text or "结果" in text
        ):
            headings.append((text, "subtitle", top, layout))
        else:
            body.append((text, "body", top, layout))
    return sorted(headings, key=lambda item: (0 if item[1] == "title" else 1, item[2])) + body


def _same_ocr_text(first: str, second: str) -> bool:
    return _same_comparable_ocr_text(
        _comparable_ocr_text(first),
        _comparable_ocr_text(second),
    )


def _comparable_ocr_text(value: str) -> str:
    return re.sub(r"[\s_—–－-]+", "", clean_ocr_text(value)).lower()


def _same_comparable_ocr_text(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    matcher = SequenceMatcher(None, left, right)
    return (
        matcher.quick_ratio() >= _OCR_TEXT_MATCH_RATIO
        and matcher.ratio() >= _OCR_TEXT_MATCH_RATIO
    )


def _index_comparable_texts(
    texts: list[str],
) -> tuple[
    list[int],
    dict[int, list[tuple[int, str]]],
    OrderedDict[int, SequenceMatcher],
]:
    """Index comparable-text lengths and create a bounded matcher cache."""

    by_length: dict[int, list[tuple[int, str]]] = {}
    for index, text in enumerate(texts):
        comparable = _comparable_ocr_text(text)
        if not comparable:
            continue
        by_length.setdefault(len(comparable), []).append((index, comparable))
    return sorted(by_length), by_length, OrderedDict()


def _index_paragraph_candidates(
    candidates: list[tuple[str, str, float, ParagraphLayout]],
) -> tuple[
    list[int],
    dict[int, list[tuple[int, str]]],
    OrderedDict[int, SequenceMatcher],
]:
    return _index_comparable_texts([candidate[0] for candidate in candidates])


def _append_indexed_text(
    texts: list[str],
    text: str,
    candidate_lengths: list[int],
    candidates_by_length: dict[int, list[tuple[int, str]]],
) -> None:
    index = len(texts)
    texts.append(text)
    _append_comparable_index(
        index,
        _comparable_ocr_text(text),
        candidate_lengths,
        candidates_by_length,
    )


def _append_comparable_index(
    index: int,
    comparable: str,
    candidate_lengths: list[int],
    candidates_by_length: dict[int, list[tuple[int, str]]],
) -> None:
    if not comparable:
        return
    length = len(comparable)
    if length not in candidates_by_length:
        candidates_by_length[length] = []
        insort(candidate_lengths, length)
    candidates_by_length[length].append((index, comparable))


def _matching_comparable_text_index(
    paragraph: str,
    candidate_lengths: list[int],
    candidates_by_length: dict[int, list[tuple[int, str]]],
    matcher_cache: OrderedDict[int, SequenceMatcher],
) -> int | None:
    """Return the first fuzzy text match, pruning impossible lengths first."""

    comparable = _comparable_ocr_text(paragraph)
    return _matching_indexed_text(
        comparable,
        candidate_lengths,
        candidates_by_length,
        matcher_cache,
    )


def _matching_indexed_text(
    comparable: str,
    candidate_lengths: list[int],
    candidates_by_length: dict[int, list[tuple[int, str]]],
    matcher_cache: OrderedDict[int, SequenceMatcher],
) -> int | None:
    if not comparable or not candidate_lengths:
        return None
    length = len(comparable)
    minimum_length = (9 * length + 10) // 11
    maximum_length = (11 * length) // 9
    start = bisect_left(candidate_lengths, minimum_length)
    end = bisect_right(candidate_lengths, maximum_length)
    candidate_groups = [
        candidates_by_length[existing_length]
        for existing_length in candidate_lengths[start:end]
    ]
    if not candidate_groups:
        return None
    entries = (
        iter(candidate_groups[0])
        if len(candidate_groups) == 1
        else merge(*candidate_groups)
    )
    for index, candidate_text in entries:
        if comparable == candidate_text:
            return index
        matcher = matcher_cache.get(index)
        if matcher is None:
            # SequenceMatcher caches the index of its second sequence. Cache
            # that work across paragraphs, but bound retained image text and
            # matcher memory on unusually dense OCR pages.
            matcher = SequenceMatcher(None, "", candidate_text)
            matcher_cache[index] = matcher
            if len(matcher_cache) > _OCR_CANDIDATE_MATCHER_CACHE_SIZE:
                matcher_cache.popitem(last=False)
        else:
            matcher_cache.move_to_end(index)
        matcher.set_seq1(comparable)
        if (
            matcher.quick_ratio() >= _OCR_TEXT_MATCH_RATIO
            and matcher.ratio() >= _OCR_TEXT_MATCH_RATIO
        ):
            return index
    return None


def _top_of_location(location: object) -> int:
    if not isinstance(location, list) or not location:
        return 0
    first = location[0]
    return int(first.get("y", 0)) if isinstance(first, dict) else 0


def _clip_ocr_bounds(
    bounds: tuple[float, float, float, float],
    *,
    image_width: float = 0.0,
    image_height: float = 0.0,
) -> tuple[float, float, float, float] | None:
    left, top, right, bottom = bounds
    if not all(isfinite(value) for value in bounds):
        return None
    left = max(left, 0.0)
    top = max(top, 0.0)
    if image_width > 0:
        right = min(right, image_width)
    if image_height > 0:
        bottom = min(bottom, image_height)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _ocr_bounds_to_word_location(
    bounds: tuple[float, float, float, float],
) -> dict[str, int]:
    left, top, right, bottom = bounds
    pixel_left = floor(left)
    pixel_top = floor(top)
    pixel_right = ceil(right)
    pixel_bottom = ceil(bottom)
    return {
        "left": pixel_left,
        "top": pixel_top,
        "width": max(pixel_right - pixel_left, 1),
        "height": max(pixel_bottom - pixel_top, 1),
    }


def _location_bounds(
    location: object,
    *,
    image_width: float = 0.0,
    image_height: float = 0.0,
) -> tuple[float, float, float, float] | None:
    """Return an OCR polygon's axis-aligned bounds in source-image pixels."""

    if not isinstance(location, list):
        return None
    points: list[tuple[float, float]] = []
    for point in location:
        if not isinstance(point, dict):
            continue
        try:
            x = float(point.get("x", 0))
            y = float(point.get("y", 0))
        except (TypeError, ValueError, OverflowError):
            return None
        if not isfinite(x) or not isfinite(y):
            return None
        points.append((x, y))
    if not points:
        return None
    left = min(x for x, _ in points)
    right = max(x for x, _ in points)
    top = min(y for _, y in points)
    bottom = max(y for _, y in points)
    return _clip_ocr_bounds(
        (left, top, right, bottom),
        image_width=image_width,
        image_height=image_height,
    )


def _cell_bounds(
    cell: dict[str, object],
    *,
    image_width: float = 0.0,
    image_height: float = 0.0,
) -> tuple[float, float, float, float] | None:
    corners = cell.get("cell_location")
    if not isinstance(corners, list) or not corners:
        return None
    points: list[tuple[float, float]] = []
    for point in corners:
        if not isinstance(point, dict):
            continue
        try:
            x = float(point.get("x", 0))
            y = float(point.get("y", 0))
        except (TypeError, ValueError, OverflowError):
            return None
        if not isfinite(x) or not isfinite(y):
            return None
        points.append((x, y))
    if not points:
        return None
    left = min(x for x, _ in points)
    right = max(x for x, _ in points)
    top = min(y for _, y in points)
    bottom = max(y for _, y in points)
    return _clip_ocr_bounds(
        (left, top, right, bottom),
        image_width=image_width,
        image_height=image_height,
    )


def _table_cell_content_lines(
    cell: dict[str, object],
    *,
    image_width: float = 0.0,
    image_height: float = 0.0,
) -> list[tuple[str, dict[str, int] | None]]:
    """Read line text and optional word boxes from the table OCR response."""

    contents = cell.get("contents")
    if not isinstance(contents, list):
        return []

    lines: list[
        tuple[str, dict[str, int] | None, tuple[float, float, float, float] | None]
    ] = []
    for item in contents:
        if not isinstance(item, dict):
            continue
        text = clean_plain_ocr_text(item.get("word", ""))
        if not text:
            continue
        bounds = _location_bounds(
            item.get("poly_location"),
            image_width=image_width,
            image_height=image_height,
        )
        location = None
        if bounds is not None:
            location = _ocr_bounds_to_word_location(bounds)
        lines.append((text, location, bounds))
    if lines and all(bounds is not None for _, _, bounds in lines):
        lines.sort(
            key=lambda item: (
                item[2][1],  # type: ignore[index]
                item[2][0],  # type: ignore[index]
            )
        )
    return [(text, location) for text, location, _ in lines]


def _table_cell_contents_text(
    lines: list[tuple[str, dict[str, int] | None]],
) -> str:
    return clean_ocr_text("\n".join(text for text, _ in lines))


def _supplement_positioned_words_from_table_contents(
    body: list[dict[str, object]],
    overrides: dict[tuple[int, int], list[tuple[str, dict[str, int] | None]]],
    positioned_words: list[tuple[str, dict[str, int]]],
    *,
    image_width: float = 0.0,
    image_height: float = 0.0,
) -> list[tuple[str, dict[str, int]]]:
    """Use fuller table-cell lines in the coordinate layer without double text."""

    bounds_by_origin: dict[tuple[int, int], tuple[float, float, float, float]] = {}
    for cell in body:
        try:
            origin = (int(cell.get("row_start", 0)), int(cell.get("col_start", 0)))
        except (TypeError, ValueError):
            continue
        bounds = _cell_bounds(
            cell,
            image_width=image_width,
            image_height=image_height,
        )
        if bounds is not None:
            bounds_by_origin[origin] = bounds

    removed_indices: set[int] = set()
    additions: list[tuple[str, dict[str, int]]] = []
    for origin, lines in overrides.items():
        bounds = bounds_by_origin.get(origin)
        if bounds is None or not lines or any(location is None for _, location in lines):
            continue
        left, top, right, bottom = bounds
        existing_indices = [
            index
            for index, (_, location) in enumerate(positioned_words)
            if left
            <= float(location.get("left", 0)) + float(location.get("width", 0)) / 2.0
            <= right
            and top
            <= float(location.get("top", 0)) + float(location.get("height", 0)) / 2.0
            <= bottom
        ]
        existing_text = _compact_ocr_sequence(
            "".join(positioned_words[index][0] for index in existing_indices)
        )
        contents_text = _compact_ocr_sequence(
            "".join(text for text, _ in lines)
        )
        if existing_indices and len(contents_text) < len(existing_text):
            continue

        removed_indices.update(existing_indices)
        additions.extend(
            (text, location)
            for text, location in lines
            if location is not None
        )
    if not additions:
        return positioned_words
    return [
        word
        for index, word in enumerate(positioned_words)
        if index not in removed_indices
    ] + additions


def _split_positioned_words_by_table_cells(
    body: list[dict[str, object]],
    positioned_words: list[tuple[str, dict[str, int]]],
    *,
    image_width: float = 0.0,
    image_height: float = 0.0,
) -> list[tuple[str, dict[str, int]]]:
    """Convert table API cells and split only clearly mapped OCR boxes."""

    geometry_cells: list[TableCell] = []
    for cell in body:
        bounds = _cell_bounds(
            cell,
            image_width=image_width,
            image_height=image_height,
        )
        if bounds is None:
            continue
        try:
            row_start = int(cell.get("row_start", 0))
            row_end = int(cell.get("row_end", row_start + 1))
            col_start = int(cell.get("col_start", 0))
            col_end = int(cell.get("col_end", col_start + 1))
        except (TypeError, ValueError):
            continue
        geometry_cells.append(
            TableCell(
                row_start=row_start,
                row_end=row_end,
                col_start=col_start,
                col_end=col_end,
                left=bounds[0],
                top=bounds[1],
                right=bounds[2],
                bottom=bounds[3],
            )
        )
    if not geometry_cells:
        return positioned_words

    return _split_positioned_words_across_cells(geometry_cells, positioned_words)


def _split_positioned_words_across_cells(
    geometry_cells: list[TableCell],
    positioned_words: list[tuple[str, dict[str, int]]],
) -> list[tuple[str, dict[str, int]]]:
    """Split cross-cell OCR boxes only when cell geometry gives one clear map."""

    if not geometry_cells:
        return positioned_words
    cell_index = _table_cell_spatial_index(geometry_cells)
    result: list[tuple[str, dict[str, int]]] = []
    for text, location in positioned_words:
        characters = list(text)
        if len(characters) < 2:
            result.append((text, location))
            continue
        left = float(location.get("left", 0))
        top = float(location.get("top", 0))
        width = max(float(location.get("width", 1)), 1.0)
        height = max(float(location.get("height", 1)), 1.0)
        right = left + width
        center_y = top + height / 2.0
        overlapping_cells = list(
            _table_cells_overlapping_horizontal_span(
                center_y,
                left,
                right,
                cell_index,
                geometry_cells,
            )
        )
        if len(overlapping_cells) < 2:
            result.append((text, location))
            continue

        char_centers = [
            left + width * (index + 0.5) / len(characters)
            for index in range(len(characters))
        ]
        indexes_by_cell: dict[int, list[int]] = {}
        cells_by_id = {id(cell): cell for cell in overlapping_cells}
        unambiguous = True
        for character_index, center_x in enumerate(char_centers):
            matches = [
                cell
                for cell in overlapping_cells
                if cell.left <= center_x <= cell.right
            ]
            if len(matches) != 1:
                unambiguous = False
                break
            indexes_by_cell.setdefault(id(matches[0]), []).append(character_index)
        if not unambiguous or len(indexes_by_cell) < 2:
            result.append((text, location))
            continue

        split_words: list[tuple[float, str, dict[str, int]]] = []
        for cell_id, indexes in indexes_by_cell.items():
            if indexes != list(range(indexes[0], indexes[-1] + 1)):
                unambiguous = False
                break
            piece = "".join(characters[index] for index in indexes).strip()
            if not piece:
                continue
            cell = cells_by_id[cell_id]
            segment_left = max(left + width * indexes[0] / len(characters), cell.left)
            segment_right = min(
                left + width * (indexes[-1] + 1) / len(characters),
                cell.right,
            )
            split_location = {
                "left": int(round(segment_left)),
                "top": int(round(top)),
                "width": max(int(round(segment_right - segment_left)), 1),
                "height": max(int(round(height)), 1),
            }
            split_words.append((cell.left, piece, split_location))
        if not unambiguous or len(split_words) < 2:
            result.append((text, location))
            continue
        result.extend(
            (piece, split_location)
            for _, piece, split_location in sorted(split_words, key=lambda item: item[0])
        )
    return result


def _layout_from_body(
    body: list[dict[str, object]],
    row_count: int,
    column_count: int,
    image_path: Path | None = None,
    *,
    image_width: float = 0.0,
    image_height: float = 0.0,
) -> TableLayout:
    """Recover grid-line positions from OCR polygons and source-image rules."""

    all_bounds = [
        _cell_bounds(
            cell,
            image_width=image_width,
            image_height=image_height,
        )
        for cell in body
    ]
    valid_bounds = [bounds for bounds in all_bounds if bounds is not None]
    if not valid_bounds:
        return TableLayout()

    table_left = min(bounds[0] for bounds in valid_bounds)
    table_right = max(bounds[2] for bounds in valid_bounds)
    table_top = min(bounds[1] for bounds in valid_bounds)
    table_bottom = max(bounds[3] for bounds in valid_bounds)
    x_boundaries, y_boundaries = _reconstructed_grid_axes(
        body,
        row_count,
        column_count,
        image_path,
        bounds_list=all_bounds,
        image_width=image_width,
        image_height=image_height,
    )

    column_widths = [max(x_boundaries[index + 1] - x_boundaries[index], 1.0) for index in range(column_count)]
    row_heights = [max(y_boundaries[index + 1] - y_boundaries[index], 1.0) for index in range(row_count)]
    source_width = 0.0
    if image_path is not None:
        try:
            with Image.open(image_path) as source_image:
                source_width = float(source_image.width)
        except OSError:
            source_width = 0.0
    return TableLayout(
        column_widths=column_widths,
        row_heights=row_heights,
        source_width=source_width,
        source_left=x_boundaries[0],
        source_right=x_boundaries[-1],
    )


def _reconstructed_grid_axes(
    body: list[dict[str, object]],
    row_count: int,
    column_count: int,
    image_path: Path | None = None,
    *,
    bounds_list: list[tuple[float, float, float, float] | None] | None = None,
    image_width: float = 0.0,
    image_height: float = 0.0,
) -> tuple[list[float], list[float]]:
    """Return grid edges, preferring real image rules over OCR estimates."""

    all_bounds = (
        bounds_list
        if bounds_list is not None
        else [
            _cell_bounds(
                cell,
                image_width=image_width,
                image_height=image_height,
            )
            for cell in body
        ]
    )
    valid_bounds = [bounds for bounds in all_bounds if bounds is not None]
    if not valid_bounds:
        return [], []
    table_left = min(bounds[0] for bounds in valid_bounds)
    table_right = max(bounds[2] for bounds in valid_bounds)
    table_top = min(bounds[1] for bounds in valid_bounds)
    table_bottom = max(bounds[3] for bounds in valid_bounds)
    x_boundaries = _axis_boundaries(body, all_bounds, column_count, horizontal=True)
    y_boundaries = _axis_boundaries(body, all_bounds, row_count, horizontal=False)
    if image_path is not None:
        detected = detect_table_line_guides(
            image_path,
            (table_left, table_top, table_right, table_bottom),
        )
        x_boundaries = _snap_boundaries(x_boundaries, detected.x)
        y_boundaries = _snap_boundaries(y_boundaries, detected.y)
    return x_boundaries, y_boundaries


def _axis_boundaries(
    body: list[dict[str, object]],
    bounds_list: list[tuple[float, float, float, float] | None],
    count: int,
    *,
    horizontal: bool,
) -> list[float]:
    buckets: list[list[float]] = [[] for _ in range(count + 1)]
    for cell, bounds in zip(body, bounds_list):
        if bounds is None:
            continue
        start_key = "col_start" if horizontal else "row_start"
        end_key = "col_end" if horizontal else "row_end"
        start = int(cell.get(start_key, 0))
        end = int(cell.get(end_key, start + 1))
        if not (0 <= start < end <= count):
            continue
        buckets[start].append(bounds[0] if horizontal else bounds[1])
        buckets[end].append(bounds[2] if horizontal else bounds[3])

    values: list[float | None] = [float(median(bucket)) if bucket else None for bucket in buckets]
    known = [index for index, value in enumerate(values) if value is not None]
    if not known:
        return [float(index) for index in range(count + 1)]

    first = known[0]
    last = known[-1]
    if first > 0:
        step = (
            (float(values[known[1]]) - float(values[first])) / (known[1] - first)
            if len(known) > 1
            else 1.0
        )
        for index in range(first - 1, -1, -1):
            values[index] = float(values[index + 1]) - step
    if last < count:
        step = (
            (float(values[last]) - float(values[known[-2]])) / (last - known[-2])
            if len(known) > 1
            else 1.0
        )
        for index in range(last + 1, count + 1):
            values[index] = float(values[index - 1]) + step

    known = [index for index, value in enumerate(values) if value is not None]
    for start_index, end_index in zip(known, known[1:]):
        if end_index == start_index + 1:
            continue
        start_value = float(values[start_index])
        end_value = float(values[end_index])
        step = (end_value - start_value) / (end_index - start_index)
        for index in range(start_index + 1, end_index):
            values[index] = start_value + step * (index - start_index)

    result = [float(value) for value in values]
    for index in range(1, len(result)):
        if result[index] <= result[index - 1]:
            result[index] = result[index - 1] + 1.0
    return result


def _snap_boundaries(estimated: list[float], detected: list[float]) -> list[float]:
    if len(estimated) < 2 or not detected:
        return estimated
    span = max(estimated[-1] - estimated[0], 1.0)
    tolerance = max(span * 0.025, 4.0)
    candidates = sorted(
        value
        for value in detected
        if estimated[0] - tolerance <= value <= estimated[-1] + tolerance
    )
    if not candidates:
        return estimated

    if len(candidates) == len(estimated) and all(
        abs(candidate - expected) <= tolerance * 1.5
        for candidate, expected in zip(candidates, estimated)
    ):
        snapped = candidates
    else:
        snapped = []
        used: set[int] = set()
        for expected in estimated:
            choices = [
                (abs(candidate - expected), index, candidate)
                for index, candidate in enumerate(candidates)
                if index not in used
            ]
            distance, index, candidate = min(choices, default=(tolerance + 1, -1, expected))
            if distance <= tolerance:
                snapped.append(candidate)
                used.add(index)
            else:
                snapped.append(expected)

    for index in range(1, len(snapped)):
        if snapped[index] <= snapped[index - 1]:
            return estimated
    return [float(value) for value in snapped]


def _overlay_positioned_words(
    grid: list[list[str]],
    body: list[dict[str, object]],
    positioned_words: list[tuple[str, dict[str, int]]],
    *,
    image_width: float = 0.0,
    image_height: float = 0.0,
) -> None:
    """Use positioned general OCR to repair missing lines inside table cells.

    The table endpoint occasionally returns only the last line of a tall cell.
    General OCR has reliable word boxes, so rebuild a cell when it gives us a
    fuller reading-order value instead of filling only blank cells.
    """
    if not positioned_words or not body:
        return

    geometry_cells: list[TableCell] = []
    body_index_by_geometry_id: dict[int, int] = {}
    for body_index, cell in enumerate(body):
        bounds = _cell_bounds(
            cell,
            image_width=image_width,
            image_height=image_height,
        )
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        geometry_cell = TableCell(
            row_start=0,
            row_end=1,
            col_start=0,
            col_end=1,
            left=left,
            top=top,
            right=right,
            bottom=bottom,
        )
        geometry_cells.append(geometry_cell)
        body_index_by_geometry_id[id(geometry_cell)] = body_index
    cell_index = _table_cell_spatial_index(geometry_cells)

    cell_words: dict[int, list[tuple[str, dict[str, int]]]] = {}
    for text, location in positioned_words:
        characters = list(text)
        if not characters:
            continue
        center_y = float(location.get("top", 0)) + float(location.get("height", 0)) / 2
        word_left = float(location.get("left", 0))
        word_width = max(float(location.get("width", 0)), 1.0)
        word_right = word_left + word_width
        character_centers = [
            word_left + word_width * (character_index + 0.5) / len(characters)
            for character_index in range(len(characters))
        ]
        for geometry_cell in _table_cells_overlapping_horizontal_span(
            center_y,
            word_left,
            word_right,
            cell_index,
            geometry_cells,
        ):
            index = body_index_by_geometry_id[id(geometry_cell)]
            left = geometry_cell.left
            right = geometry_cell.right
            overlap_left = max(left, word_left)
            overlap_right = min(right, word_right)

            # General OCR sometimes treats text on both sides of a thin table
            # line as one word. Split the word by the cell's horizontal pixel
            # range instead of assigning the whole word by its center.
            selected = [
                character
                for character, character_center in zip(characters, character_centers)
                if overlap_left <= character_center <= overlap_right
            ]
            piece = "".join(selected).strip()
            if piece:
                cell_words.setdefault(index, []).append((piece, location))

    for index, words in cell_words.items():
        cell = body[index]
        row = int(cell.get("row_start", -1))
        column = int(cell.get("col_start", -1))
        if not (0 <= row < len(grid) and 0 <= column < len(grid[row])):
            continue
        words.sort(key=lambda item: (item[1].get("top", 0), item[1].get("left", 0)))
        lines: list[list[tuple[str, dict[str, int]]]] = []
        for word in words:
            top = word[1].get("top", 0)
            height = max(word[1].get("height", 1), 1)
            if not lines or abs(top - lines[-1][0][1].get("top", 0)) > height * 0.65:
                lines.append([word])
            else:
                lines[-1].append(word)
        candidate = clean_ocr_text(
            "\n".join("".join(word[0] for word in line) for line in lines).strip()
        )
        existing = clean_ocr_text(grid[row][column])
        # Equal-length alternatives are often a general-OCR character variant
        # (for example a person's name). Keep the table endpoint's cell text
        # in that case and only replace it when positioned OCR supplies more
        # characters/lines.
        if candidate and len(candidate.replace("\n", "")) > len(existing.replace("\n", "")):
            grid[row][column] = candidate


def _generic_positioned_words(
    payload: dict[str, object],
    *,
    preserve_content: bool = False,
    image_width: int = 0,
    image_height: int = 0,
    recognized_texts: list[str] | None = None,
    unpositioned_texts: list[str] | None = None,
) -> list[tuple[str, dict[str, int]]]:
    """Normalize common coordinate-OCR response shapes.

    Self-hosted OCR gateways do not all use Baidu's ``words_result`` name.
    Accept the small set of equivalent names used by common adapters so the
    editor can still preserve title and cell coordinates when ``OCR_PROVIDER``
    is ``generic_http``.
    """

    word_lists: list[list[object]] = []
    for key in ("positioned_words", "words_result", "words", "results"):
        candidate = payload.get(key)
        if isinstance(candidate, list) and candidate:
            word_lists.append(candidate)
    if not word_lists:
        return []

    clean_text = clean_plain_ocr_text if preserve_content else clean_ocr_text
    fallback_texts: list[str] = []
    for raw in word_lists:
        positioned: list[tuple[str, dict[str, int]]] = []
        current_texts: list[str] = []
        current_unpositioned: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                raw_text = (
                    item.get("text")
                    or item.get("words")
                    or item.get("content")
                    or item.get("value")
                    or ""
                )
            else:
                raw_text = item
            text = clean_text(raw_text) if raw_text else ""
            if not text:
                continue
            current_texts.append(text)
            location = (
                _generic_word_location(
                    item,
                    image_width=image_width,
                    image_height=image_height,
                )
                if isinstance(item, dict)
                else None
            )
            if location is not None:
                positioned.append((text, location))
            else:
                current_unpositioned.append(text)

        if positioned:
            if recognized_texts is not None:
                recognized_texts.extend(current_texts)
            if unpositioned_texts is not None:
                unpositioned_texts.extend(current_unpositioned)
            return positioned
        if current_texts and not fallback_texts:
            fallback_texts = current_texts

    if recognized_texts is not None:
        recognized_texts.extend(fallback_texts)
    if unpositioned_texts is not None:
        unpositioned_texts.extend(fallback_texts)
    return []


def _generic_word_location(
    item: dict[str, object],
    *,
    image_width: int = 0,
    image_height: int = 0,
) -> dict[str, int] | None:
    """Convert bbox/location/left-top-width-height variants to one box."""

    def normalized_box(
        left: object,
        top: object,
        width: object,
        height: object,
    ) -> dict[str, int] | None:
        try:
            x1 = float(left)
            y1 = float(top)
            box_width = float(width)
            box_height = float(height)
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            not all(isfinite(value) for value in (x1, y1, box_width, box_height))
            or box_width <= 0
            or box_height <= 0
        ):
            return None

        if (
            image_width > 0
            and image_height > 0
            and -_NORMALIZED_OCR_COORDINATE_TOLERANCE <= x1 <= 1.25
            and -_NORMALIZED_OCR_COORDINATE_TOLERANCE <= y1 <= 1.25
            and 0.0 < box_width <= 1.5
            and 0.0 < box_height <= 1.5
            and x1 + box_width <= 1.5
            and y1 + box_height <= 1.5
        ):
            # Vision gateways often report normalized coordinates in [0, 1].
            # Convert those to the source-image pixel frame before clipping.
            x1 *= image_width
            y1 *= image_height
            box_width *= image_width
            box_height *= image_height

        x2 = x1 + box_width
        y2 = y1 + box_height
        if not isfinite(x2) or not isfinite(y2):
            return None
        x1 = max(x1, 0.0)
        y1 = max(y1, 0.0)
        if image_width > 0:
            x2 = min(x2, float(image_width))
        if image_height > 0:
            y2 = min(y2, float(image_height))
        if x2 <= x1 or y2 <= y1:
            return None
        return {
            "left": int(round(x1)),
            "top": int(round(y1)),
            "width": max(int(round(x2 - x1)), 1),
            "height": max(int(round(y2 - y1)), 1),
        }

    for location_key in ("location", "bbox", "box", "polygon"):
        raw_location = item.get(location_key)
        if isinstance(raw_location, dict):
            left = raw_location.get("left", raw_location.get("x"))
            top = raw_location.get("top", raw_location.get("y"))
            width = raw_location.get("width", raw_location.get("w"))
            height = raw_location.get("height", raw_location.get("h"))
            try:
                if width is None and raw_location.get("right") is not None and left is not None:
                    width = float(raw_location["right"]) - float(left)
                if height is None and raw_location.get("bottom") is not None and top is not None:
                    height = float(raw_location["bottom"]) - float(top)
            except (TypeError, ValueError, OverflowError):
                continue
            if left is not None and top is not None and width is not None and height is not None:
                location = normalized_box(left, top, width, height)
                if location is not None:
                    return location
        elif isinstance(raw_location, list):
            # A four-number bbox is conventionally [left, top, width, height].
            if len(raw_location) == 4:
                try:
                    coordinates = tuple(float(value) for value in raw_location)
                except (TypeError, ValueError, OverflowError):
                    coordinates = ()
                if coordinates:
                    location = normalized_box(*coordinates)
                    if location is not None:
                        return location
            bounds = _location_bounds(raw_location)
            if bounds is not None:
                left, top, right, bottom = bounds
                location = normalized_box(left, top, right - left, bottom - top)
                if location is not None:
                    return location

    # Some gateways put geometry beside the text rather than under location.
    try:
        left = item.get("left", item.get("x"))
        top = item.get("top", item.get("y"))
        width = item.get("width", item.get("w"))
        height = item.get("height", item.get("h"))
        if left is not None and top is not None and width is not None and height is not None:
            return normalized_box(left, top, width, height)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _bounded_generic_tables(value: object) -> list[list[list[str]]]:
    """Keep generic-provider grids within the limits used by native exports."""

    if not isinstance(value, list):
        return []
    tables: list[list[list[str]]] = []
    for table in value:
        if not isinstance(table, list):
            tables.append([])
            continue
        row_count = len(table)
        if row_count > _MAX_OCR_TABLE_DIMENSION:
            logger.warning("Ignoring oversized generic OCR table (%s rows)", row_count)
            tables.append([])
            continue
        column_count = max(
            (
                len(row)
                if isinstance(row, list)
                else 1 if isinstance(row, str) and row else 0
                for row in table
            ),
            default=0,
        )
        if (
            column_count > _MAX_OCR_TABLE_DIMENSION
            or row_count * column_count > _MAX_OCR_TABLE_GRID_CELLS
        ):
            logger.warning(
                "Ignoring oversized generic OCR table grid (%s rows, %s columns)",
                row_count,
                column_count,
            )
            tables.append([])
            continue
        if column_count == 0:
            tables.append([])
            continue

        normalized_rows: list[list[str]] = []
        for row in table:
            if isinstance(row, list):
                normalized_rows.append([
                    _generic_table_cell_text(cell)
                    for cell in row
                ])
            elif isinstance(row, str):
                normalized_rows.append([clean_ocr_text(row)])
            else:
                normalized_rows.append([])
        tables.append(normalized_rows)
    return tables


def _generic_table_cell_text(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and not isfinite(value):
        return ""
    if isinstance(value, (str, int, float)):
        return clean_ocr_text(value)
    return ""


def _generic_table_layouts(
    value: object,
    image_width: int,
    image_height: int = 0,
) -> list[TableLayout]:
    if not isinstance(value, list):
        return []
    layouts: list[TableLayout] = []
    for item in value:
        if not isinstance(item, dict):
            # Keep one layout slot per table so a malformed earlier entry
            # cannot shift later tables' widths and source positions.
            layouts.append(TableLayout())
            continue
        def numbers(key: str) -> list[float]:
            raw = item.get(key, [])
            if not isinstance(raw, list):
                return []
            result: list[float] = []
            for number in raw:
                try:
                    parsed = float(number)
                except (TypeError, ValueError, OverflowError):
                    parsed = 0.0
                result.append(parsed if isfinite(parsed) and parsed > 0 else 0.0)
            return result

        def number(key: str) -> float:
            try:
                parsed = float(item.get(key, 0) or 0)
            except (TypeError, ValueError, OverflowError):
                return 0.0
            return parsed if isfinite(parsed) else 0.0

        source_width_value = number("source_width")
        source_width = source_width_value
        normalized_x_coordinates = image_width > 1 and source_width_value <= 1
        if source_width <= 0 or normalized_x_coordinates:
            source_width = max(float(image_width), 0.0)
        source_left = max(number("source_left"), 0.0)
        source_right = max(number("source_right"), 0.0)
        column_widths = numbers("column_widths")
        row_heights = numbers("row_heights")
        if image_width > 1:
            if (
                normalized_x_coordinates
                and source_left <= 1
                and source_right <= 1
                and source_right > source_left
            ):
                source_left *= image_width
                source_right *= image_width
            if column_widths and max(column_widths) <= 1:
                column_widths = [width * image_width for width in column_widths]
        if image_height > 1 and row_heights and max(row_heights) <= 1:
            row_heights = [height * image_height for height in row_heights]
        if source_width > 0:
            source_left = min(source_left, source_width)
            source_right = min(source_right, source_width)
        if source_right <= source_left:
            source_left = source_right = 0.0
        layouts.append(
            TableLayout(
                column_widths=column_widths,
                row_heights=row_heights,
                source_width=source_width,
                source_left=source_left,
                source_right=source_right,
            )
        )
    return layouts


def _generic_table_cell_bounds(
    item: dict[str, object],
    *,
    image_width: int,
    image_height: int,
) -> tuple[float, float, float, float] | None:
    """Read generic table-cell boxes in pixels or normalized image coordinates."""

    location = _generic_word_location(
        item,
        image_width=image_width,
        image_height=image_height,
    )
    if location is not None:
        left = float(location["left"])
        top = float(location["top"])
        return _clip_ocr_bounds(
            (
                left,
                top,
                left + location["width"],
                top + location["height"],
            ),
            image_width=image_width,
            image_height=image_height,
        )

    nested_locations = [
        item.get(key)
        for key in ("cell_location", "polygon", "location", "bbox", "box")
    ]
    for raw_location in nested_locations:
        if not isinstance(raw_location, list):
            continue
        bounds = _location_bounds(
            raw_location,
            image_width=image_width,
            image_height=image_height,
        )
        if bounds is None:
            if len(raw_location) != 4:
                continue
            try:
                left, top, width, height = (float(value) for value in raw_location)
            except (TypeError, ValueError, OverflowError):
                continue
            if not all(isfinite(value) for value in (left, top, width, height)):
                continue
            bounds = left, top, left + width, top + height
        return _normalize_generic_cell_bounds(
            bounds,
            image_width=image_width,
            image_height=image_height,
        )

    # Table-cell APIs often publish right/bottom while positioned-word APIs
    # publish width/height. Accept both forms without losing the grid span.
    geometries = [item]
    for key in ("location", "bbox", "box"):
        location = item.get(key)
        if isinstance(location, dict):
            geometries.append(location)
    for geometry in geometries:
        left_value = geometry.get("left")
        if left_value is None:
            left_value = geometry.get("x")
        top_value = geometry.get("top")
        if top_value is None:
            top_value = geometry.get("y")
        try:
            left = float(left_value)
            top = float(top_value)
            right_value = geometry.get("right")
            bottom_value = geometry.get("bottom")
            width_value = geometry.get("width")
            if width_value is None:
                width_value = geometry.get("w")
            height_value = geometry.get("height")
            if height_value is None:
                height_value = geometry.get("h")
            right = (
                float(right_value)
                if right_value is not None
                else left + float(width_value)
            )
            bottom = (
                float(bottom_value)
                if bottom_value is not None
                else top + float(height_value)
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(isfinite(value) for value in (left, top, right, bottom)):
            continue
        return _normalize_generic_cell_bounds(
            (left, top, right, bottom),
            image_width=image_width,
            image_height=image_height,
        )
    return None


def _normalize_generic_cell_bounds(
    bounds: tuple[float, float, float, float],
    *,
    image_width: int,
    image_height: int,
) -> tuple[float, float, float, float] | None:
    left, top, right, bottom = bounds
    if (
        image_width > 1
        and -_NORMALIZED_OCR_COORDINATE_TOLERANCE <= left <= 1.25
        and -_NORMALIZED_OCR_COORDINATE_TOLERANCE <= right <= 1.5
        and right > left
    ):
        left *= image_width
        right *= image_width
    if (
        image_height > 1
        and -_NORMALIZED_OCR_COORDINATE_TOLERANCE <= top <= 1.25
        and -_NORMALIZED_OCR_COORDINATE_TOLERANCE <= bottom <= 1.5
        and bottom > top
    ):
        top *= image_height
        bottom *= image_height
    return _clip_ocr_bounds(
        (left, top, right, bottom),
        image_width=image_width,
        image_height=image_height,
    )


def _generic_table_cells(
    value: object,
    *,
    image_width: int = 0,
    image_height: int = 0,
    table_shapes: list[tuple[int, int]] | None = None,
) -> list[list[TableCell]]:
    if not isinstance(value, list):
        return []
    # The wire format is a list per table, with one cell object per table.
    tables: list[list[TableCell]] = []
    for table_index, table_value in enumerate(value):
        if not isinstance(table_value, list):
            tables.append([])
            continue
        if len(table_value) > _MAX_OCR_TABLE_GRID_CELLS:
            logger.warning("Ignoring oversized generic OCR cell list")
            tables.append([])
            continue
        if table_shapes is not None and table_index < len(table_shapes):
            row_limit, column_limit = table_shapes[table_index]
            if row_limit <= 0 or column_limit <= 0:
                tables.append([])
                continue
        else:
            row_limit = column_limit = 0
        cells: list[TableCell] = []
        for item in table_value:
            if not isinstance(item, dict):
                continue
            row_start = _table_index(item.get("row_start", 0))
            col_start = _table_index(item.get("col_start", 0))
            if row_start is None or col_start is None:
                continue
            row_end = (
                _table_index(item["row_end"])
                if "row_end" in item
                else row_start + 1
            )
            col_end = (
                _table_index(item["col_end"])
                if "col_end" in item
                else col_start + 1
            )
            if row_end is None or col_end is None:
                continue
            row_start = max(row_start, 0)
            col_start = max(col_start, 0)
            if row_limit > 0 and column_limit > 0:
                row_end = min(row_end, row_limit)
                col_end = min(col_end, column_limit)
            elif (
                row_end > _MAX_OCR_TABLE_DIMENSION
                or col_end > _MAX_OCR_TABLE_DIMENSION
            ):
                continue
            if (
                row_end <= row_start
                or col_end <= col_start
                or row_start >= _MAX_OCR_TABLE_DIMENSION
                or col_start >= _MAX_OCR_TABLE_DIMENSION
            ):
                continue
            clipped = _generic_table_cell_bounds(
                item,
                image_width=image_width,
                image_height=image_height,
            )
            if clipped is None:
                left = top = right = bottom = 0.0
            else:
                left, top, right, bottom = clipped
            cells.append(
                TableCell(
                    row_start=row_start,
                    row_end=row_end,
                    col_start=col_start,
                    col_end=col_end,
                    text=clean_ocr_text(item.get("text", "")),
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                )
            )
        tables.append(cells)
    return tables


def _tesseract_positioned_words(
    image_path: Path,
    *,
    page_segmentation_mode: int = 6,
    thresholding_method: int | None = None,
    low_confidence_words: list[tuple[float, str]] | None = None,
    word_confidences: list[tuple[float, str]] | None = None,
    timeout_seconds: int = 45,
) -> list[tuple[str, dict[str, int]]]:
    """Get local text boxes when a configured OCR service returns text only."""

    executable = os.getenv("TESSERACT_CMD", "tesseract").strip() or "tesseract"
    languages = os.getenv("OCR_LAYOUT_LANGUAGES", "chi_sim+eng").strip() or "chi_sim+eng"
    try:
        with Image.open(image_path) as image:
            image_width, image_height = image.size
    except (OSError, ValueError):
        image_width = image_height = 0
    try:
        command = [
            executable,
            str(image_path),
            "stdout",
            "-l",
            languages,
            "--dpi",
            str(_TESSERACT_DPI),
        ]
        if thresholding_method is not None:
            command.extend(["-c", f"thresholding_method={thresholding_method}"])
        command.extend(["--psm", str(page_segmentation_mode), "tsv"])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout:
        return []

    positioned: list[tuple[str, dict[str, int]]] = []
    try:
        rows = csv.DictReader(io.StringIO(result.stdout), delimiter="\t")
        for row in rows:
            if row.get("level") != "5":
                continue
            text = clean_plain_ocr_text(row.get("text", ""))
            if not text:
                continue
            try:
                confidence = float(row.get("conf", "-1"))
                left = int(row.get("left", "0"))
                top = int(row.get("top", "0"))
                width = int(row.get("width", "0"))
                height = int(row.get("height", "0"))
            except (TypeError, ValueError):
                continue
            if not isfinite(confidence) or confidence < 0 or width <= 0 or height <= 0:
                continue
            box_left = max(left, 0)
            box_top = max(top, 0)
            box_right = left + width
            box_bottom = top + height
            if image_width > 0:
                box_right = min(box_right, image_width)
            if image_height > 0:
                box_bottom = min(box_bottom, image_height)
            if box_right <= box_left or box_bottom <= box_top:
                continue
            if word_confidences is not None:
                word_confidences.append((confidence, text))
            if (
                confidence < _TESSERACT_LOW_CONFIDENCE_THRESHOLD
                and low_confidence_words is not None
            ):
                low_confidence_words.append((confidence, text))
            positioned.append((
                text,
                {
                    "left": box_left,
                    "top": box_top,
                    "width": box_right - box_left,
                    "height": box_bottom - box_top,
                    "confidence": int(round(confidence)),
                },
            ))
            if len(positioned) >= 10000:
                break
    except (csv.Error, UnicodeError):
        return []
    return positioned


def _tesseract_positioned_words_with_sparse_fallback(
    image_path: Path,
    *,
    thresholding_method: int | None = None,
    timeout_seconds: int = 45,
    low_confidence_words: list[tuple[float, str]] | None = None,
    word_confidences: list[tuple[float, str]] | None = None,
) -> list[tuple[str, dict[str, int]]]:
    """Use sparse segmentation when PSM 6 misses text or scores it poorly."""
    default_low_confidence: list[tuple[float, str]] = []
    default_confidences: list[tuple[float, str]] = []
    words = _tesseract_positioned_words(
        image_path,
        thresholding_method=thresholding_method,
        timeout_seconds=timeout_seconds,
        low_confidence_words=default_low_confidence,
        word_confidences=default_confidences,
    )
    selected_low_confidence = default_low_confidence
    selected_confidences = default_confidences
    if not words:
        words = _tesseract_positioned_words(
            image_path,
            page_segmentation_mode=11,
            thresholding_method=thresholding_method,
            timeout_seconds=min(timeout_seconds, 15),
            low_confidence_words=selected_low_confidence,
            word_confidences=selected_confidences,
        )
    elif default_confidences:
        low_ratio = len(default_low_confidence) / len(default_confidences)
        mean_confidence = sum(
            score for score, _text in default_confidences
        ) / len(default_confidences)
        if (
            low_ratio >= 0.25
            or mean_confidence < _TESSERACT_LOW_CONFIDENCE_THRESHOLD
        ):
            sparse_low_confidence: list[tuple[float, str]] = []
            sparse_confidences: list[tuple[float, str]] = []
            sparse_words = _tesseract_positioned_words(
                image_path,
                page_segmentation_mode=11,
                thresholding_method=thresholding_method,
                low_confidence_words=sparse_low_confidence,
                word_confidences=sparse_confidences,
                timeout_seconds=min(timeout_seconds, 15),
            )
            if (
                sparse_words
                and _should_replace_tesseract_candidate(
                    sparse_confidences,
                    default_confidences,
                )
            ):
                words = sparse_words
                selected_low_confidence = sparse_low_confidence
                selected_confidences = sparse_confidences

    if low_confidence_words is not None:
        low_confidence_words.extend(selected_low_confidence)
    if word_confidences is not None:
        word_confidences.extend(selected_confidences)
    return words


def _tesseract_orientation_correction(image_path: Path) -> int:
    """Return a confident Tesseract OSD correction in the editor's angle convention."""
    executable = os.getenv("TESSERACT_CMD", "tesseract").strip() or "tesseract"
    try:
        result = subprocess.run(
            [
                executable,
                str(image_path),
                "stdout",
                "--dpi",
                str(_TESSERACT_DPI),
                "--psm",
                "0",
                "osd",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if result.returncode != 0 or not result.stdout:
        return 0

    rotation_match = re.search(r"(?m)^\s*Rotate:\s*(0|90|180|270)\s*$", result.stdout)
    confidence_match = re.search(
        r"(?m)^\s*Orientation confidence:\s*([-+]?\d+(?:\.\d+)?)\s*$",
        result.stdout,
    )
    if not rotation_match or not confidence_match:
        return 0
    try:
        confidence = float(confidence_match.group(1))
    except (TypeError, ValueError, OverflowError):
        return 0
    if not isfinite(confidence) or confidence < _TESSERACT_OSD_CONFIDENCE_THRESHOLD:
        return 0

    # Tesseract's Rotate value is clockwise; Pillow's correction field uses
    # negative degrees for clockwise rotation, matching Baidu's convention.
    return {
        "0": 0,
        "90": -90,
        "180": 180,
        "270": 90,
    }[rotation_match.group(1)]


def _tesseract_recognition_quality(scores: list[tuple[float, str]]) -> float:
    """Score recognized characters while strongly discounting weak tokens."""
    return sum(
        (confidence / 100.0) ** 2 * max(len(_comparable_ocr_text(text)), 1)
        for confidence, text in scores
    )


def _should_replace_tesseract_candidate(
    candidate_scores: list[tuple[float, str]],
    current_scores: list[tuple[float, str]],
    *,
    coverage_reference_scores: list[tuple[float, str]] | None = None,
    minimum_quality_ratio: float = 1.05,
    minimum_coverage_ratio: float = 0.75,
    strong_quality_ratio: float = 1.35,
) -> bool:
    """Require confidence gains without allowing cumulative text loss."""

    if not candidate_scores:
        return False
    if not current_scores:
        return True

    candidate_quality = _tesseract_recognition_quality(candidate_scores)
    current_quality = _tesseract_recognition_quality(current_scores)
    if current_quality > 0:
        if candidate_quality < current_quality * minimum_quality_ratio:
            return False
    elif candidate_quality <= current_quality:
        return False

    reference_scores = (
        coverage_reference_scores
        if coverage_reference_scores is not None
        else current_scores
    )
    candidate_characters = sum(
        len(_comparable_ocr_text(text)) for _confidence, text in candidate_scores
    )
    reference_characters = sum(
        len(_comparable_ocr_text(text)) for _confidence, text in reference_scores
    )
    if reference_characters <= 0:
        return candidate_characters > 0
    if candidate_characters >= reference_characters * minimum_coverage_ratio:
        return True
    return candidate_quality >= current_quality * strong_quality_ratio


def _tesseract_low_confidence_examples(
    words: list[tuple[float, str]],
    *,
    limit: int = 5,
) -> list[str]:
    """Choose the lowest-confidence unique tokens for the import review hint."""
    if limit <= 0:
        return []
    examples: list[str] = []
    seen: set[str] = set()
    for _confidence, text in sorted(words, key=lambda item: item[0]):
        comparable = _comparable_ocr_text(text)
        if not comparable or comparable in seen:
            continue
        seen.add(comparable)
        examples.append(text)
        if len(examples) >= limit:
            break
    return examples


def _normalized_text_with_offsets(value: str) -> tuple[str, list[str], list[int]]:
    text = clean_ocr_text(value)
    characters: list[str] = []
    offsets: list[int] = []
    for index, raw_character in enumerate(text):
        for character in unicodedata.normalize("NFKC", raw_character).casefold():
            if character.isspace() or unicodedata.category(character).startswith("P"):
                continue
            characters.append(character)
            offsets.append(index)
    return text, characters, offsets


def _recover_table_cell_word_boxes(
    cell: TableCell,
    local_words: list[tuple[str, dict[str, int]]],
) -> list[PositionedWord]:
    """Map exact local OCR tokens to a table cell's authoritative text."""
    source_text, source_chars, source_offsets = _normalized_text_with_offsets(cell.text)
    if (
        not source_chars
        or len(source_chars) > 10000
        or cell.right <= cell.left
        or cell.bottom <= cell.top
    ):
        return []

    candidates: list[tuple[str, dict[str, int], list[str]]] = []
    local_character_count = 0
    for text, location in local_words:
        normalized = _normalized_text_with_offsets(text)[1]
        if not normalized:
            continue
        left = float(location.get("left", 0))
        top = float(location.get("top", 0))
        width = max(float(location.get("width", 1)), 1.0)
        height = max(float(location.get("height", 1)), 1.0)
        center_x = left + width / 2.0
        center_y = top + height / 2.0
        if cell.left <= center_x <= cell.right and cell.top <= center_y <= cell.bottom:
            candidates.append((text, location, normalized))
            local_character_count += len(normalized)
            if local_character_count > 10000:
                return []
    # Tesseract TSV already follows page, line and word reading order. Keep
    # that order; sorting only by each token's top edge can reorder glyphs on
    # the same slightly skewed line.
    local_chars: list[str] = []
    local_word_indices: list[int] = []
    for word_index, (_, _, token_chars) in enumerate(candidates):
        local_chars.extend(token_chars)
        local_word_indices.extend([word_index] * len(token_chars))
    if not local_chars or len(local_chars) > 10000:
        return []

    source_word_indices: list[int | None]
    if local_chars == source_chars:
        source_word_indices = list(local_word_indices)
    else:
        source_counts = Counter(source_chars)
        local_counts = Counter(local_chars)
        candidate_pairs = sum(
            count * local_counts.get(character, 0)
            for character, count in source_counts.items()
        )
        if candidate_pairs > _MAX_OCR_ALIGNMENT_CANDIDATE_PAIRS:
            return []

        matcher = SequenceMatcher(None, source_chars, local_chars, autojunk=False)
        source_word_indices = [None] * len(source_chars)
        matched_source: set[int] = set()
        matched_local: set[int] = set()
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                source_index = block.a + offset
                local_index = block.b + offset
                source_word_indices[source_index] = local_word_indices[local_index]
                matched_source.add(source_index)
                matched_local.add(local_index)

        # A one or two character OCR discrepancy is common on long table
        # cells. Require strong coverage in both directions before trusting
        # local boxes, so unrelated text never becomes an erasure mask.
        if (
            len(matched_source) / len(source_chars) < 0.8
            or len(matched_local) / len(local_chars) < 0.8
            or not matched_source
        ):
            return []

        matched_indices = sorted(matched_source)
        for source_index, word_index in enumerate(source_word_indices):
            if word_index is not None:
                continue
            next_match = bisect_left(matched_indices, source_index)
            if next_match == 0:
                anchor = matched_indices[0]
            elif next_match == len(matched_indices):
                anchor = matched_indices[-1]
            else:
                previous_anchor = matched_indices[next_match - 1]
                next_anchor = matched_indices[next_match]
                anchor = (
                    previous_anchor
                    if source_index - previous_anchor <= next_anchor - source_index
                    else next_anchor
                )
            source_word_indices[source_index] = source_word_indices[anchor]

        # Keep source text runs in reading order even if repeated characters
        # gave the sequence matcher an ambiguous local anchor.
        previous_word = 0
        for source_index, word_index in enumerate(source_word_indices):
            current_word = max(previous_word, int(word_index or 0))
            source_word_indices[source_index] = current_word
            previous_word = current_word

    groups: list[tuple[int, int, int]] = []
    for source_index, word_index in enumerate(source_word_indices):
        if word_index is None:
            continue
        if groups and groups[-1][0] == word_index and groups[-1][2] == source_index:
            previous_word, start, _ = groups[-1]
            groups[-1] = (previous_word, start, source_index + 1)
        else:
            groups.append((word_index, source_index, source_index + 1))

    recovered: list[PositionedWord] = []
    raw_offset = 0
    for group_index, (word_index, _start, end) in enumerate(groups):
        raw_end = (
            source_offsets[end - 1] + 1
            if group_index < len(groups) - 1
            else len(source_text)
        )
        word_text = source_text[raw_offset:raw_end].strip()
        _, location, _ = candidates[word_index]
        if word_text:
            recovered.append(
                PositionedWord(
                    text=word_text,
                    left=float(location.get("left", 0)),
                    top=float(location.get("top", 0)),
                    width=max(float(location.get("width", 1)), 1.0),
                    height=max(float(location.get("height", 1)), 1.0),
                )
            )
        raw_offset = raw_end
    return recovered


def _supplement_table_positions_from_local_ocr(
    result: OcrResult,
    image_path: Path,
) -> None:
    """Recover missing table word boxes only when local OCR exactly aligns."""
    cells: list[TableCell] = [cell for group in result.table_cells for cell in group]
    text_cells = [cell for cell in cells if clean_ocr_text(cell.text)]
    if not text_cells:
        return
    words_by_cell = _positioned_words_by_table_cell(text_cells, result.positioned_words)
    incomplete_cells: list[TableCell] = []
    for cell in text_cells:
        existing = words_by_cell.get(id(cell), [])
        existing_text = "".join(
            character
            for word in existing
            for character in _normalized_text_with_offsets(word.text)[1]
        )
        expected_text = "".join(_normalized_text_with_offsets(cell.text)[1])
        if existing_text != expected_text:
            incomplete_cells.append(cell)
    if not incomplete_cells:
        return

    recovered_cells: list[tuple[TableCell, list[PositionedWord]]] = []
    unrecovered_cells: list[TableCell] = []

    def recover_with(
        local_words: list[tuple[str, dict[str, int]]],
        cells: list[TableCell],
    ) -> None:
        local_words_by_cell: dict[int, list[tuple[str, dict[str, int]]]] = {}
        cell_index = _table_cell_spatial_index(cells)
        for word in local_words:
            text, location = word
            left = float(location.get("left", 0))
            top = float(location.get("top", 0))
            width = max(float(location.get("width", 1)), 1.0)
            height = max(float(location.get("height", 1)), 1.0)
            center_x = left + width / 2.0
            center_y = top + height / 2.0
            for cell in _table_cells_containing_center(center_x, center_y, cell_index):
                local_words_by_cell.setdefault(id(cell), []).append(word)
        for cell in cells:
            recovered = _recover_table_cell_word_boxes(
                cell,
                local_words_by_cell.get(id(cell), []),
            )
            if recovered:
                recovered_cells.append((cell, recovered))
            else:
                unrecovered_cells.append(cell)

    local_words = _tesseract_positioned_words(image_path)
    recover_with(local_words, incomplete_cells)
    if unrecovered_cells:
        # Sparse page segmentation can find text that PSM 6 misses between
        # form rules and checkbox graphics. Retry only the cells that still
        # lack a high-coverage alignment.
        still_unrecovered = unrecovered_cells
        unrecovered_cells = []
        sparse_words = _tesseract_positioned_words(image_path, page_segmentation_mode=11)
        recover_with(sparse_words, still_unrecovered)
    if not recovered_cells:
        return

    recovered_cell_index = _table_cell_spatial_index(
        [cell for cell, _ in recovered_cells],
    )

    def belongs_to_recovered_cell(word: PositionedWord) -> bool:
        center_x = float(word.left) + float(word.width) / 2.0
        center_y = float(word.top) + float(word.height) / 2.0
        return next(
            _table_cells_containing_center(center_x, center_y, recovered_cell_index),
            None,
        ) is not None

    result.positioned_words = [
        word for word in result.positioned_words if not belongs_to_recovered_cell(word)
    ]
    seen: set[tuple[str, float, float, float, float]] = {
        (
            clean_ocr_text(word.text),
            float(word.left),
            float(word.top),
            float(word.width),
            float(word.height),
        )
        for word in result.positioned_words
    }
    for _, recovered in recovered_cells:
        for word in recovered:
            key = (
                clean_ocr_text(word.text),
                float(word.left),
                float(word.top),
                float(word.width),
                float(word.height),
            )
            if key not in seen:
                result.positioned_words.append(word)
                seen.add(key)


def _word_center_in_table_cell(word: PositionedWord, cell: TableCell) -> bool:
    center_x = word.left + word.width / 2.0
    center_y = word.top + word.height / 2.0
    return _table_center_is_in_cell(center_x, center_y, cell)


def _table_center_is_in_cell(center_x: float, center_y: float, cell: TableCell) -> bool:
    return cell.left <= center_x <= cell.right and cell.top <= center_y <= cell.bottom


def _table_cell_spatial_index(
    cells: list[TableCell],
) -> tuple[
    dict[tuple[int, int], list[tuple[int, TableCell]]],
    list[tuple[int, TableCell]],
]:
    """Bucket cells by page coordinates; keep huge or non-finite cells in overflow."""
    index: dict[tuple[int, int], list[tuple[int, TableCell]]] = {}
    overflow: list[tuple[int, TableCell]] = []
    cell_size = _TABLE_CELL_SPATIAL_INDEX_CELL_SIZE
    for cell_index, cell in enumerate(cells):
        if cell.right < cell.left or cell.bottom < cell.top:
            continue
        indexed_cell = (cell_index, cell)
        bounds = (cell.left, cell.top, cell.right, cell.bottom)
        if not all(isfinite(float(bound)) for bound in bounds):
            overflow.append(indexed_cell)
            continue

        left_start = floor(cell.left / cell_size)
        left_end = floor(cell.right / cell_size)
        top_start = floor(cell.top / cell_size)
        top_end = floor(cell.bottom / cell_size)
        bucket_count = (left_end - left_start + 1) * (top_end - top_start + 1)
        if bucket_count > _TABLE_CELL_SPATIAL_INDEX_MAX_BUCKETS:
            overflow.append(indexed_cell)
            continue
        for left_bucket in range(left_start, left_end + 1):
            for top_bucket in range(top_start, top_end + 1):
                index.setdefault((left_bucket, top_bucket), []).append(indexed_cell)
    return index, overflow


def _table_cells_containing_center(
    center_x: float,
    center_y: float,
    index: tuple[
        dict[tuple[int, int], list[tuple[int, TableCell]]],
        list[tuple[int, TableCell]],
    ],
) -> Iterator[TableCell]:
    cells_by_area, overflow = index
    candidates = ()
    if isfinite(center_x) and isfinite(center_y):
        cell_size = _TABLE_CELL_SPATIAL_INDEX_CELL_SIZE
        key = (floor(center_x / cell_size), floor(center_y / cell_size))
        candidates = cells_by_area.get(key, ())

    seen: set[int] = set()
    for _, cell in chain(candidates, overflow):
        identity = id(cell)
        if identity in seen:
            continue
        seen.add(identity)
        if _table_center_is_in_cell(center_x, center_y, cell):
            yield cell


def _table_cells_overlapping_horizontal_span(
    center_y: float,
    left: float,
    right: float,
    index: tuple[
        dict[tuple[int, int], list[tuple[int, TableCell]]],
        list[tuple[int, TableCell]],
    ],
    all_cells: list[TableCell],
) -> Iterator[TableCell]:
    """Return cells intersecting a word's horizontal span in original order."""
    cells_by_area, overflow = index
    if not all(isfinite(value) for value in (center_y, left, right)) or right <= left:
        candidate_entries = enumerate(all_cells)
    else:
        cell_size = _TABLE_CELL_SPATIAL_INDEX_CELL_SIZE
        x_start = floor(left / cell_size)
        x_end = floor(right / cell_size)
        y_bucket = floor(center_y / cell_size)
        if x_end - x_start + 1 > _TABLE_CELL_SPATIAL_INDEX_MAX_BUCKETS:
            candidate_entries = enumerate(all_cells)
        else:
            entries_by_order = {
                cell_index: cell
                for x_bucket in range(x_start, x_end + 1)
                for cell_index, cell in cells_by_area.get((x_bucket, y_bucket), ())
            }
            entries_by_order.update({cell_index: cell for cell_index, cell in overflow})
            candidate_entries = sorted(entries_by_order.items())

    for _, cell in candidate_entries:
        if (
            cell.top <= center_y <= cell.bottom
            and min(right, cell.right) > max(left, cell.left)
        ):
            yield cell


def _positioned_words_by_table_cell(
    cells: list[TableCell],
    words: list[PositionedWord],
) -> dict[int, list[PositionedWord]]:
    """Group page OCR words by the cells containing their centers."""
    cell_index = _table_cell_spatial_index(cells)
    words_by_cell: dict[int, list[PositionedWord]] = {}
    for word in words:
        center_x = float(word.left) + float(word.width) / 2.0
        center_y = float(word.top) + float(word.height) / 2.0
        for cell in _table_cells_containing_center(center_x, center_y, cell_index):
            words_by_cell.setdefault(id(cell), []).append(word)
    return words_by_cell


def _map_paragraphs_to_local_boxes(
    paragraphs: list[str],
    local_words: list[tuple[str, dict[str, int]]],
) -> list[tuple[str, dict[str, int]]]:
    """Use local OCR geometry only when it aligns reliably with provider text."""

    def normalized_chars(value: str) -> list[str]:
        return [
            character.casefold()
            # OCR engines may disagree on compatibility glyphs such as full-width
            # Latin letters/digits while still reading the same source region.
            # Normalize only the matching key; keep provider text untouched.
            for character in unicodedata.normalize("NFKC", clean_ocr_text(value))
            if not character.isspace()
            and not unicodedata.category(character).startswith("P")
        ]

    provider_chars: list[str] = []
    provider_paragraphs: list[int] = []
    paragraph_lengths: list[int] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        chars = normalized_chars(paragraph)
        if not chars:
            return []
        provider_chars.extend(chars)
        provider_paragraphs.extend([paragraph_index] * len(chars))
        paragraph_lengths.append(len(chars))

    local_chars: list[str] = []
    local_word_indices: list[int] = []
    for word_index, (text, _location) in enumerate(local_words):
        chars = normalized_chars(text)
        local_chars.extend(chars)
        local_word_indices.extend([word_index] * len(chars))
    # Keep the quadratic sequence matcher bounded on very large or noisy pages.
    if (
        not provider_chars
        or not local_chars
        or len(provider_chars) > 10000
        or len(local_chars) > 10000
    ):
        return []

    matched_provider: list[set[int]] = [set() for _ in paragraphs]
    matched_words: list[set[int]] = [set() for _ in paragraphs]

    common_prefix = 0
    common_limit = min(len(provider_chars), len(local_chars))
    while (
        common_prefix < common_limit
        and provider_chars[common_prefix] == local_chars[common_prefix]
    ):
        common_prefix += 1

    common_suffix = 0
    suffix_limit = min(
        len(provider_chars) - common_prefix,
        len(local_chars) - common_prefix,
    )
    while (
        common_suffix < suffix_limit
        and provider_chars[len(provider_chars) - common_suffix - 1]
        == local_chars[len(local_chars) - common_suffix - 1]
    ):
        common_suffix += 1

    def add_match(provider_index: int, local_index: int) -> None:
        paragraph_index = provider_paragraphs[provider_index]
        matched_provider[paragraph_index].add(provider_index)
        matched_words[paragraph_index].add(local_word_indices[local_index])

    # Peel exact ends in linear time. This handles exact repetitive scans and
    # small OCR differences without asking SequenceMatcher to compare the full page.
    for provider_index in range(common_prefix):
        add_match(provider_index, provider_index)
    for offset in range(common_suffix):
        provider_index = len(provider_chars) - common_suffix + offset
        local_index = len(local_chars) - common_suffix + offset
        add_match(provider_index, local_index)

    provider_middle_end = len(provider_chars) - common_suffix
    local_middle_end = len(local_chars) - common_suffix
    provider_middle = provider_chars[common_prefix:provider_middle_end]
    local_middle = local_chars[common_prefix:local_middle_end]
    if provider_middle and local_middle:
        provider_counts = Counter(provider_middle)
        local_counts = Counter(local_middle)
        candidate_pairs = sum(
            count * local_counts.get(character, 0)
            for character, count in provider_counts.items()
        )
        # SequenceMatcher visits each same-character pair. Repeated OCR noise
        # can otherwise make a page with only a few thousand characters slow.
        # If the middle alone exceeds budget, retain only these exact anchors;
        # the coverage thresholds below decide whether they locate every paragraph.
        if candidate_pairs <= _MAX_OCR_ALIGNMENT_CANDIDATE_PAIRS and candidate_pairs:
            matcher = SequenceMatcher(None, provider_middle, local_middle, autojunk=False)
            for block in matcher.get_matching_blocks():
                for offset in range(block.size):
                    provider_index = common_prefix + block.a + offset
                    local_index = common_prefix + block.b + offset
                    add_match(provider_index, local_index)

    total_matched = sum(len(indices) for indices in matched_provider)
    if total_matched / len(provider_chars) < 0.65:
        return []

    mapped: list[tuple[str, dict[str, int]]] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        if len(matched_provider[paragraph_index]) / paragraph_lengths[paragraph_index] < 0.45:
            return []
        boxes = [local_words[index][1] for index in matched_words[paragraph_index]]
        if not boxes:
            return []
        left = min(box["left"] for box in boxes)
        top = min(box["top"] for box in boxes)
        right = max(box["left"] + box["width"] for box in boxes)
        bottom = max(box["top"] + box["height"] for box in boxes)
        mapped.append((
            paragraph,
            {
                "left": max(left, 0),
                "top": max(top, 0),
                "width": max(right - left, 1),
                "height": max(bottom - top, 1),
            },
        ))
    return mapped


def _tesseract_layout_for_paragraphs(
    paragraphs: list[str],
    image_path: Path,
) -> tuple[list[tuple[str, dict[str, int]]], list[tuple[str, dict[str, int]]]]:
    """Prefer the page mode whose local text aligns with the cloud paragraphs."""
    default_words = _tesseract_positioned_words(image_path)
    default_mapped = _map_paragraphs_to_local_boxes(paragraphs, default_words) if paragraphs else []
    if default_mapped or (default_words and not paragraphs):
        return default_words, default_mapped

    sparse_words = _tesseract_positioned_words(image_path, page_segmentation_mode=11)
    sparse_mapped = _map_paragraphs_to_local_boxes(paragraphs, sparse_words)
    if sparse_mapped or (sparse_words and not paragraphs):
        return sparse_words, sparse_mapped
    if not paragraphs:
        return default_words or sparse_words, []

    # Cloud text remains authoritative. When default and sparse local OCR
    # cannot align its boxes, try tiled Otsu for unevenly lit scans and only
    # use the resulting geometry if it maps back to the supplied paragraphs.
    tiled_otsu_words = _tesseract_positioned_words_with_sparse_fallback(
        image_path,
        thresholding_method=_TESSERACT_LEPTONICA_OTSU_THRESHOLDING_METHOD,
        timeout_seconds=8,
    )
    tiled_otsu_mapped = _map_paragraphs_to_local_boxes(paragraphs, tiled_otsu_words)
    if tiled_otsu_mapped:
        return tiled_otsu_words, tiled_otsu_mapped

    return default_words or sparse_words or tiled_otsu_words, []


@lru_cache(maxsize=1)
def get_ocr_provider() -> OcrProvider:
    provider = os.getenv("OCR_PROVIDER", "mock").strip().lower()
    if provider == "mock":
        return MockOcrProvider()
    if provider == "generic_http":
        endpoint = os.getenv("OCR_ENDPOINT", "").strip()
        if not endpoint:
            raise RuntimeError("OCR_PROVIDER=generic_http 时必须配置 OCR_ENDPOINT")
        return GenericHttpOcrProvider(endpoint, os.getenv("OCR_API_KEY"))
    raise RuntimeError(
        f"不支持的 OCR_PROVIDER: {provider}；仅支持 mock（本地开发）和 "
        "generic_http（自建 OCR 服务）"
    )


def _is_page_corner_guide_noise(
    text: str,
    location: dict[str, int],
    image_width: int,
    image_height: int,
) -> bool:
    """Discard faint crop-mark fragments recognized in the page corners."""

    token = text.strip()
    if (
        not token
        or len(token) > 4
        or not any(character in "[]|_" for character in token)
        or any(
            not (character.isascii() and (character.isalpha() or character in "[]|_"))
            for character in token
        )
    ):
        return False
    try:
        confidence = float(location.get("confidence"))
        left = float(location.get("left"))
        top = float(location.get("top"))
        width = float(location.get("width"))
        height = float(location.get("height"))
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        not all(isfinite(value) for value in (confidence, left, top, width, height))
        or confidence > 85
        or image_width <= 0
        or image_height <= 0
        or left < 0
        or top < 0
        or width <= 0
        or height <= 0
        or width > image_width * 0.08
        or height > image_height * 0.05
    ):
        return False
    near_horizontal_edge = (
        top <= image_height * 0.04
        or top + height >= image_height * 0.96
    )
    near_vertical_edge = (
        left <= image_width * 0.12
        or left + width >= image_width * 0.88
    )
    return near_horizontal_edge and near_vertical_edge


def _local_ocr_result(
    image_path: Path,
    positioned_words: list[tuple[str, dict[str, int]]],
    low_confidence_scores: list[tuple[float, str]],
    *,
    orientation_correction_degrees: int = 0,
) -> OcrResult:
    try:
        with Image.open(image_path) as image:
            image_width, image_height = image.size
    except (OSError, ValueError) as exc:
        raise RuntimeError("无法读取待识别图片") from exc

    corner_guide_tokens = set()
    filtered_positioned_words = []
    for text, location in positioned_words:
        if _is_page_corner_guide_noise(text, location, image_width, image_height):
            corner_guide_tokens.add(text.strip())
        else:
            filtered_positioned_words.append((text, location))
    positioned_words = filtered_positioned_words
    if corner_guide_tokens:
        low_confidence_scores = [
            (confidence, text)
            for confidence, text in low_confidence_scores
            if not (text.strip() in corner_guide_tokens and confidence <= 85)
        ]

    if not positioned_words:
        raise OcrNoTextRecognized(
            "本机 OCR 没有识别到可编辑文字",
            image_width=image_width,
            image_height=image_height,
            orientation_correction_degrees=orientation_correction_degrees,
        )
    low_confidence_examples = _tesseract_low_confidence_examples(low_confidence_scores)

    table_result = _table_from_image_lines(image_path, positioned_words)
    if table_result is not None:
        table_result.low_text_confidence = bool(low_confidence_examples)
        table_result.low_confidence_examples = low_confidence_examples
        table_result.orientation_correction_degrees = orientation_correction_degrees
        return table_result

    paragraphs = _positioned_paragraph_candidates(
        positioned_words,
        image_width=image_width,
        preserve_content=True,
    )
    if not paragraphs:
        raise OcrNoTextRecognized(
            "本机 OCR 没有识别到可编辑文字",
            image_width=image_width,
            image_height=image_height,
            orientation_correction_degrees=orientation_correction_degrees,
        )
    return OcrResult(
        image_width=image_width,
        image_height=image_height,
        paragraphs=[item[0] for item in paragraphs],
        paragraph_styles=[item[1] for item in paragraphs],
        paragraph_layouts=[item[3] for item in paragraphs],
        low_text_confidence=bool(low_confidence_examples),
        low_confidence_examples=low_confidence_examples,
        orientation_correction_degrees=orientation_correction_degrees,
        positioned_words=_as_positioned_word_objects(
            positioned_words,
            preserve_content=True,
        ),
    )


def _write_autocontrast_ocr_candidate(source_path: Path, destination_path: Path) -> None:
    """Create a same-size grayscale candidate for faded, low-contrast scans."""
    with Image.open(source_path) as source:
        grayscale = ImageOps.grayscale(source)
        try:
            enhanced = ImageOps.autocontrast(grayscale)
            try:
                dpi = source.info.get("dpi")
                save_options: dict[str, object] = {"compress_level": 1}
                if (
                    isinstance(dpi, tuple)
                    and len(dpi) == 2
                    and all(isinstance(value, (int, float)) for value in dpi)
                ):
                    save_options["dpi"] = dpi
                enhanced.save(destination_path, format="PNG", **save_options)
            finally:
                enhanced.close()
        finally:
            grayscale.close()


def _write_inverted_ocr_candidate(
    source_path: Path,
    destination_path: Path,
) -> bool:
    """Invert light text on dark pages; return false for ordinary paper scans."""
    with Image.open(source_path) as source:
        grayscale = ImageOps.grayscale(source)
        try:
            histogram = grayscale.histogram()
            pixel_count = sum(histogram)
            if pixel_count <= 0:
                return False
            dark_fraction = sum(histogram[:96]) / pixel_count
            light_fraction = sum(histogram[160:]) / pixel_count
            if dark_fraction < 0.55 or light_fraction < 0.03:
                return False

            inverted = ImageOps.invert(grayscale)
            try:
                save_options: dict[str, object] = {"compress_level": 1}
                dpi = source.info.get("dpi")
                if (
                    isinstance(dpi, tuple)
                    and len(dpi) == 2
                    and all(isinstance(value, (int, float)) for value in dpi)
                ):
                    save_options["dpi"] = dpi
                inverted.save(destination_path, format="PNG", **save_options)
            finally:
                inverted.close()
            return True
        finally:
            grayscale.close()


def _write_clahe_ocr_candidate(source_path: Path, destination_path: Path) -> None:
    """Create a same-size local-contrast candidate for unevenly lit scans."""
    import cv2
    import numpy as np

    with Image.open(source_path) as source:
        grayscale = ImageOps.grayscale(source)
        try:
            try:
                enhanced_pixels = cv2.createCLAHE(
                    clipLimit=2.0,
                    tileGridSize=(8, 8),
                ).apply(np.asarray(grayscale))
            except cv2.error as exc:
                raise ValueError("Could not enhance local OCR contrast") from exc
            enhanced = Image.fromarray(enhanced_pixels)
            try:
                save_options: dict[str, object] = {"compress_level": 1}
                dpi = source.info.get("dpi")
                if (
                    isinstance(dpi, tuple)
                    and len(dpi) == 2
                    and all(isinstance(value, (int, float)) for value in dpi)
                ):
                    save_options["dpi"] = dpi
                enhanced.save(destination_path, format="PNG", **save_options)
            finally:
                enhanced.close()
        finally:
            grayscale.close()


def _write_table_line_removed_ocr_candidate(
    source_path: Path,
    destination_path: Path,
) -> bool:
    """Remove clear table rules while preserving the OCR image coordinates."""
    import cv2
    import numpy as np

    encoded = np.fromfile(str(source_path), dtype=np.uint8)
    grayscale = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
    if grayscale is None or grayscale.size == 0:
        raise ValueError("Could not read the table OCR candidate")

    image_height, image_width = grayscale.shape
    if (
        image_width < 64
        or image_height < 64
        or image_width * image_height > 12_000_000
    ):
        return False

    binary = cv2.threshold(
        grayscale,
        210,
        255,
        cv2.THRESH_BINARY_INV,
    )[1]
    horizontal_length = min(image_width, max(32, image_width // 15))
    vertical_length = min(image_height, max(40, image_height // 15))
    horizontal = cv2.morphologyEx(
        binary,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (horizontal_length, 1)),
    )
    vertical = cv2.morphologyEx(
        binary,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, vertical_length)),
    )

    def long_rule_mask(mask, *, horizontal_rule: bool):
        contours, _hierarchy = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        selected = np.zeros_like(mask)
        count = 0
        for contour in contours:
            _x, _y, width, height = cv2.boundingRect(contour)
            extent = width if horizontal_rule else height
            limit = image_width if horizontal_rule else image_height
            if extent >= max(40, limit * 0.45):
                cv2.drawContours(selected, [contour], -1, 255, thickness=cv2.FILLED)
                count += 1
        return selected, count

    # Requiring repeated rules in both directions avoids touching ordinary
    # text, underlines, and single decorative separators.
    horizontal, horizontal_rule_count = long_rule_mask(
        horizontal,
        horizontal_rule=True,
    )
    vertical, vertical_rule_count = long_rule_mask(
        vertical,
        horizontal_rule=False,
    )
    if horizontal_rule_count < 2 or vertical_rule_count < 2:
        return False

    line_mask = cv2.bitwise_or(horizontal, vertical)
    line_mask = cv2.dilate(
        line_mask,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )
    try:
        cleaned = cv2.inpaint(grayscale, line_mask, 3, cv2.INPAINT_TELEA)
        encoded_success, encoded_candidate = cv2.imencode(".png", cleaned)
        if not encoded_success:
            raise ValueError("Could not encode the table OCR candidate")
        encoded_candidate.tofile(str(destination_path))
    except cv2.error as exc:
        raise ValueError("Could not remove table rules from the OCR candidate") from exc
    return True


def recognize_locally(image_path: Path) -> OcrResult:
    """Build an editable OCR result with local Tesseract as a last resort."""

    def recognize_candidate(
        path: Path,
        *,
        thresholding_method: int | None = None,
        timeout_seconds: int | None = None,
        page_segmentation_mode: int | None = None,
    ) -> tuple[
        list[tuple[str, dict[str, int]]],
        list[tuple[float, str]],
        list[tuple[float, str]],
    ]:
        low_confidence_scores: list[tuple[float, str]] = []
        word_confidences: list[tuple[float, str]] = []
        candidate_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else 15 if thresholding_method is not None else 45
        )
        if page_segmentation_mode is None:
            positioned_words = _tesseract_positioned_words_with_sparse_fallback(
                path,
                thresholding_method=thresholding_method,
                timeout_seconds=candidate_timeout,
                low_confidence_words=low_confidence_scores,
                word_confidences=word_confidences,
            )
        else:
            positioned_words = _tesseract_positioned_words(
                path,
                page_segmentation_mode=page_segmentation_mode,
                thresholding_method=thresholding_method,
                timeout_seconds=candidate_timeout,
                low_confidence_words=low_confidence_scores,
                word_confidences=word_confidences,
            )
        return positioned_words, low_confidence_scores, word_confidences

    def best_result(
        path: Path,
        words: list[tuple[str, dict[str, int]]],
        low_scores: list[tuple[float, str]],
        scores: list[tuple[float, str]],
        *,
        orientation_degrees: int = 0,
    ) -> OcrResult:
        mean_confidence = (
            sum(score for score, _text in scores) / len(scores)
            if scores
            else 100.0
        )
        needs_alternative_processing = not words or (
            bool(low_scores)
            and bool(scores)
            and (
                len(low_scores) / len(scores) >= 0.25
                or mean_confidence < _TESSERACT_LOW_CONFIDENCE_THRESHOLD
            )
        )
        selected_words = words
        selected_low = low_scores
        selected_scores = scores
        if needs_alternative_processing:
            # Leptonica's tiled Otsu can retain more detail than global Otsu
            # on scans with uneven lighting. It preserves the source image
            # dimensions, so OCR boxes remain in the same coordinate space.
            tiled_otsu_words, tiled_otsu_low, tiled_otsu_scores = recognize_candidate(
                path,
                thresholding_method=_TESSERACT_LEPTONICA_OTSU_THRESHOLDING_METHOD,
                timeout_seconds=8,
            )
            if tiled_otsu_words and (
                not selected_words
                or _should_replace_tesseract_candidate(
                    tiled_otsu_scores,
                    selected_scores,
                    coverage_reference_scores=scores,
                )
            ):
                selected_words = tiled_otsu_words
                selected_low = tiled_otsu_low
                selected_scores = tiled_otsu_scores

            adaptive_words, adaptive_low, adaptive_scores = recognize_candidate(
                path,
                thresholding_method=_TESSERACT_ADAPTIVE_THRESHOLDING_METHOD,
            )
            if (
                adaptive_words
                and (
                    not selected_words
                    or _should_replace_tesseract_candidate(
                        adaptive_scores,
                        selected_scores,
                        coverage_reference_scores=scores,
                    )
                )
            ):
                selected_words = adaptive_words
                selected_low = adaptive_low
                selected_scores = adaptive_scores

            with tempfile.TemporaryDirectory(
                prefix="iscan-local-ocr-contrast-"
            ) as temporary_dir:
                lines_removed_path = Path(temporary_dir) / "table-lines-removed.png"
                try:
                    has_table_rules = _write_table_line_removed_ocr_candidate(
                        path,
                        lines_removed_path,
                    )
                except (ImportError, OSError, ValueError):
                    logger.warning("Could not prepare the table-line OCR candidate")
                else:
                    if has_table_rules:
                        lines_removed_words, lines_removed_low, lines_removed_scores = (
                            recognize_candidate(
                                lines_removed_path,
                                timeout_seconds=10,
                                page_segmentation_mode=11,
                            )
                        )
                        if lines_removed_words and (
                            not selected_words
                            or _should_replace_tesseract_candidate(
                                lines_removed_scores,
                                selected_scores,
                                minimum_quality_ratio=0.95,
                                minimum_coverage_ratio=0.9,
                            )
                        ):
                            selected_words = lines_removed_words
                            selected_low = lines_removed_low
                            selected_scores = lines_removed_scores

                contrast_path = Path(temporary_dir) / "autocontrast.png"
                try:
                    _write_autocontrast_ocr_candidate(path, contrast_path)
                except (OSError, ValueError):
                    logger.warning("Could not prepare the autocontrast OCR candidate")
                else:
                    contrast_words, contrast_low, contrast_scores = recognize_candidate(
                        contrast_path,
                        timeout_seconds=10,
                    )
                    if (
                        contrast_words
                        and (
                            not selected_words
                            or _should_replace_tesseract_candidate(
                                contrast_scores,
                                selected_scores,
                                coverage_reference_scores=scores,
                            )
                        )
                    ):
                        selected_words = contrast_words
                        selected_low = contrast_low
                        selected_scores = contrast_scores

                if not selected_words or selected_low:
                    inverted_path = Path(temporary_dir) / "inverted.png"
                    try:
                        has_dark_background = _write_inverted_ocr_candidate(
                            path,
                            inverted_path,
                        )
                    except (OSError, ValueError):
                        logger.warning("Could not prepare the inverted OCR candidate")
                    else:
                        if has_dark_background:
                            inverted_words, inverted_low, inverted_scores = recognize_candidate(
                                inverted_path,
                                timeout_seconds=10,
                            )
                            if inverted_words and (
                                not selected_words
                                or _should_replace_tesseract_candidate(
                                    inverted_scores,
                                    selected_scores,
                                    coverage_reference_scores=scores,
                                )
                            ):
                                selected_words = inverted_words
                                selected_low = inverted_low
                                selected_scores = inverted_scores

                if not selected_words or selected_low:
                    clahe_path = Path(temporary_dir) / "clahe.png"
                    try:
                        _write_clahe_ocr_candidate(path, clahe_path)
                    except (ImportError, OSError, ValueError):
                        logger.warning("Could not prepare the local-contrast OCR candidate")
                    else:
                        clahe_words, clahe_low, clahe_scores = recognize_candidate(
                            clahe_path,
                            timeout_seconds=10,
                        )
                        if clahe_words and (
                            not selected_words
                            or _should_replace_tesseract_candidate(
                                clahe_scores,
                                selected_scores,
                                coverage_reference_scores=scores,
                            )
                        ):
                            selected_words = clahe_words
                            selected_low = clahe_low
                            selected_scores = clahe_scores

        return _local_ocr_result(
            path,
            selected_words,
            selected_low,
            orientation_correction_degrees=orientation_degrees,
        )

    orientation_correction_degrees = _tesseract_orientation_correction(image_path)
    baseline_words, baseline_low, baseline_scores = recognize_candidate(image_path)
    if not orientation_correction_degrees:
        return best_result(image_path, baseline_words, baseline_low, baseline_scores)

    with tempfile.TemporaryDirectory(prefix="iscan-local-ocr-upright-") as temporary_dir:
        upright_path = Path(temporary_dir) / "upright.png"
        try:
            _rotate_ocr_image_copy(image_path, upright_path, orientation_correction_degrees)
        except (OSError, ValueError):
            logger.warning(
                "Could not prepare the orientation-corrected local OCR candidate"
            )
            return best_result(image_path, baseline_words, baseline_low, baseline_scores)
        upright_words, upright_low, upright_scores = recognize_candidate(upright_path)
        if upright_words and (
            not baseline_words
            or _should_replace_tesseract_candidate(
                upright_scores,
                baseline_scores,
                minimum_quality_ratio=0.95,
            )
        ):
            return best_result(
                upright_path,
                upright_words,
                upright_low,
                upright_scores,
                orientation_degrees=orientation_correction_degrees,
            )

    return best_result(image_path, baseline_words, baseline_low, baseline_scores)
