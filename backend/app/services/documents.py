from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
from bisect import bisect_left, insort
from collections.abc import Iterable, Iterator
from itertools import chain
from math import ceil, floor, isfinite
from pathlib import Path
from statistics import median
from typing import NamedTuple
from urllib.parse import urlsplit

from lxml import etree
from PIL import Image, ImageDraw, ImageFont
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from app.services.image_color import _image_on_white, _image_to_srgb_on_white
from app.services.editor import (
    _build_source_word_spatial_index,
    _match_source_word_index,
    sample_paper_color,
    sample_source_text_color,
)
from app.services.ocr import (
    OcrResult,
    ParagraphLayout,
    PositionedWord,
    TableCell,
    TableLayout,
    TextFragment,
    clean_ocr_text,
    clean_plain_ocr_text,
    normalize_table_cell_value,
)
from app.services.table_geometry import LineGuides, detect_table_line_guides


WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
EMU_PER_PT = 12700
A4_SHORT_SIDE_PT = 595.276
A4_LONG_SIDE_PT = 841.89
MAX_PDF_PAGE_HEIGHT_PT = 14_400
PRINT_EXPORT_DPI = 300
PRINT_EXPORT_MAX_IMAGE_EDGE = 4096


class _NativeCheckboxLayout(NamedTuple):
    text: str
    left_indent_pt: float
    top_padding_pt: float
    checkbox_tab_stops: list[float]
    paragraph_texts: list[str]
    line_spacing_pt: float
    font_size_pt: float
    footer_tab_position_pt: float | None
    footer_space_after_pt: float


def _image_page_size(source_width: int, source_height: int) -> tuple[float, float]:
    """Fit a scan inside the matching portrait or landscape A4 page bounds."""
    if source_width > source_height:
        max_width_pt, max_height_pt = A4_LONG_SIDE_PT, A4_SHORT_SIDE_PT
    else:
        max_width_pt, max_height_pt = A4_SHORT_SIDE_PT, A4_LONG_SIDE_PT
    scale = min(
        max_width_pt / max(source_width, 1),
        max_height_pt / max(source_height, 1),
    )
    return source_width * scale, source_height * scale


def _set_image_page_size(section, width_pt: float, height_pt: float) -> None:
    section.orientation = (
        WD_ORIENT.LANDSCAPE if width_pt > height_pt else WD_ORIENT.PORTRAIT
    )
    section.page_width = Pt(width_pt)
    section.page_height = Pt(height_pt)


def _print_export_image_size(
    source_width: int,
    source_height: int,
    page_width_pt: float,
    page_height_pt: float,
) -> tuple[int, int]:
    """Bound embedded scan pixels to print resolution and safe image edges."""
    width = max(int(source_width), 1)
    height = max(int(source_height), 1)
    target_width = max(1, int(round(page_width_pt * PRINT_EXPORT_DPI / 72)))
    target_height = max(1, int(round(page_height_pt * PRINT_EXPORT_DPI / 72)))
    scale = min(
        1.0,
        target_width / width,
        target_height / height,
        PRINT_EXPORT_MAX_IMAGE_EDGE / max(width, height),
    )
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _source_ink_rgb(
    image: Image.Image,
    word: PositionedWord,
    original_words: list[PositionedWord] | None = None,
    source_word_index: tuple[dict[tuple[int, int], list[int]], float] | None = None,
) -> tuple[int, int, int]:
    sample_word = word
    if original_words:
        source_word_match = _match_source_word_index(word, original_words, source_word_index)
        if source_word_match is not None:
            sample_word = original_words[source_word_match]
    color = sample_source_text_color(
        image,
        (
            sample_word.left,
            sample_word.top,
            sample_word.left + sample_word.width,
            sample_word.top + sample_word.height,
        ),
    )
    if not color or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return (0, 0, 0)
    return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))


def _merge_coordinate_words(
    requested_words: list[PositionedWord],
    original_words: list[PositionedWord],
    table_cells: list[list[TableCell]],
) -> list[PositionedWord]:
    """Keep table OCR words when the coordinate editor submits only overlays.

    The mobile coordinate editor intentionally omits words covered by a table
    cell and submits the edited cell through ``tables`` instead. Replacing the
    complete OCR coordinate list with that partial list would make every
    untouched table cell lose its editable Word textbox after one edit.
    """

    if not requested_words:
        return [
            PositionedWord(
                word.text,
                word.left,
                word.top,
                word.width,
                word.height,
                is_form_field=word.is_form_field,
            )
            for word in original_words
        ]

    merged = [
        PositionedWord(
            word.text,
            word.left,
            word.top,
            word.width,
            word.height,
            is_form_field=word.is_form_field,
        )
        for word in requested_words
    ]
    merged_index = _coordinate_word_index(merged)
    table_cell_index = _table_cell_coordinate_index(table_cells)
    for original in original_words:
        center_x = original.left + original.width / 2.0
        center_y = original.top + original.height / 2.0
        if not _table_contains_center(center_x, center_y, table_cell_index):
            continue
        already_present = any(
            abs(merged[index].left - original.left) <= _COORDINATE_MATCH_TOLERANCE
            and abs(merged[index].top - original.top) <= _COORDINATE_MATCH_TOLERANCE
            and abs(merged[index].width - original.width) <= _COORDINATE_SIZE_TOLERANCE
            and abs(merged[index].height - original.height) <= _COORDINATE_SIZE_TOLERANCE
            for index in _coordinate_word_candidates(original, merged_index)
        )
        if not already_present:
            new_index = len(merged)
            merged.append(
                PositionedWord(
                    original.text,
                    original.left,
                    original.top,
                    original.width,
                    original.height,
                    is_form_field=original.is_form_field,
                )
            )
            key = _coordinate_word_key(original)
            if key is not None:
                merged_index.setdefault(key, []).append(new_index)
    return merged


_COORDINATE_MATCH_TOLERANCE = 1.5
_COORDINATE_SIZE_TOLERANCE = 2.0
_COORDINATE_INDEX_CELL_SIZE = 2.0
_TABLE_CELL_INDEX_CELL_SIZE = 128.0
_TABLE_CELL_INDEX_MAX_BUCKETS = 256


def _coordinate_word_key(word: PositionedWord) -> tuple[int, int] | None:
    left = float(word.left)
    top = float(word.top)
    if not isfinite(left) or not isfinite(top):
        return None
    return (
        floor(left / _COORDINATE_INDEX_CELL_SIZE),
        floor(top / _COORDINATE_INDEX_CELL_SIZE),
    )


def _coordinate_word_index(words: list[PositionedWord]) -> dict[tuple[int, int], list[int]]:
    """Index source boxes by small coordinate cells for tolerant lookup."""

    index: dict[tuple[int, int], list[int]] = {}
    for word_index, word in enumerate(words):
        key = _coordinate_word_key(word)
        if key is None:
            continue
        index.setdefault(key, []).append(word_index)
    return index


def _coordinate_word_candidates(
    target: PositionedWord,
    index: dict[tuple[int, int], list[int]],
) -> Iterator[int]:
    """Yield nearby indexed boxes without scanning unrelated coordinates."""

    left = float(target.left)
    top = float(target.top)
    if not isfinite(left) or not isfinite(top):
        return
    tolerance = _COORDINATE_MATCH_TOLERANCE
    cell_size = _COORDINATE_INDEX_CELL_SIZE
    left_cells = range(
        floor((left - tolerance) / cell_size),
        floor((left + tolerance) / cell_size) + 1,
    )
    top_cells = range(
        floor((top - tolerance) / cell_size),
        floor((top + tolerance) / cell_size) + 1,
    )
    for left_cell in left_cells:
        for top_cell in top_cells:
            yield from index.get((left_cell, top_cell), ())


def _table_cell_coordinate_index(
    table_cells: list[list[TableCell]],
    include_degenerate: bool = False,
) -> tuple[dict[tuple[int, int], list[TableCell]], list[TableCell]]:
    """Bucket table cells by page area, keeping large boxes in a small fallback."""

    index: dict[tuple[int, int], list[TableCell]] = {}
    overflow: list[TableCell] = []
    cell_size = _TABLE_CELL_INDEX_CELL_SIZE
    for row in table_cells:
        for cell in row:
            has_positive_area = cell.right > cell.left and cell.bottom > cell.top
            if not has_positive_area and (
                not include_degenerate
                or not (cell.right >= cell.left and cell.bottom >= cell.top)
            ):
                continue
            bounds = (cell.left, cell.top, cell.right, cell.bottom)
            if not all(isfinite(float(bound)) for bound in bounds):
                overflow.append(cell)
                continue

            left_start = floor(cell.left / cell_size)
            left_end = floor(cell.right / cell_size)
            top_start = floor(cell.top / cell_size)
            top_end = floor(cell.bottom / cell_size)
            bucket_count = (left_end - left_start + 1) * (top_end - top_start + 1)
            if bucket_count > _TABLE_CELL_INDEX_MAX_BUCKETS:
                overflow.append(cell)
                continue
            for left_bucket in range(left_start, left_end + 1):
                for top_bucket in range(top_start, top_end + 1):
                    index.setdefault((left_bucket, top_bucket), []).append(cell)
    return index, overflow


def _table_contains_center(
    center_x: float,
    center_y: float,
    index: tuple[dict[tuple[int, int], list[TableCell]], list[TableCell]],
) -> bool:
    return any(
        cell.left <= center_x <= cell.right
        and cell.top <= center_y <= cell.bottom
        for cell in _table_cells_at_center(center_x, center_y, index)
    )


def _table_cells_at_center(
    center_x: float,
    center_y: float,
    index: tuple[dict[tuple[int, int], list[TableCell]], list[TableCell]],
) -> Iterator[TableCell]:
    cells_by_area, overflow = index
    candidates = ()
    if isfinite(center_x) and isfinite(center_y):
        cell_size = _TABLE_CELL_INDEX_CELL_SIZE
        key = (floor(center_x / cell_size), floor(center_y / cell_size))
        candidates = cells_by_area.get(key, ())
    yield from chain(candidates, overflow)


def _table_cell_memberships(
    table_cells: list[list[TableCell]],
    words: list[PositionedWord],
    require_text: bool = False,
) -> set[tuple[int, int, int, int]]:
    """Return exact cell identities touched by positioned words."""

    memberships: set[tuple[int, int, int, int]] = set()
    candidate_words = [
        word for word in words
        if not require_text or clean_ocr_text(word.text)
    ]
    for table_index, cells in enumerate(table_cells):
        index = _table_cell_coordinate_index([cells])
        for word in candidate_words:
            center_x = word.left + word.width / 2.0
            center_y = word.top + word.height / 2.0
            for cell in _table_cells_at_center(center_x, center_y, index):
                if (
                    cell.left <= center_x <= cell.right
                    and cell.top <= center_y <= cell.bottom
                ):
                    memberships.add(
                        (table_index, cell.row_start, cell.col_start, id(cell))
                    )
    return memberships


def _positioned_words_by_table_cell(
    table_cells: list[list[TableCell]],
    words: list[PositionedWord],
    cleaned_text_by_word_id: dict[int, str] | None = None,
) -> dict[int, list[PositionedWord]]:
    """Group text-bearing OCR boxes by cell and optionally cache cleaned text."""

    if not table_cells or not words:
        return {}
    words_by_cell: dict[int, list[PositionedWord]] = {}
    text_words = []
    for word in words:
        cleaned = clean_ocr_text(word.text)
        if not cleaned:
            continue
        if cleaned_text_by_word_id is not None:
            cleaned_text_by_word_id[id(word)] = cleaned
        text_words.append(word)
    for cells in table_cells:
        index = _table_cell_coordinate_index([cells], include_degenerate=True)
        for word in text_words:
            center_x = float(word.left) + float(word.width) / 2.0
            center_y = float(word.top) + float(word.height) / 2.0
            for cell in _table_cells_at_center(center_x, center_y, index):
                if (
                    cell.left <= center_x <= cell.right
                    and cell.top <= center_y <= cell.bottom
                ):
                    words_by_cell.setdefault(id(cell), []).append(word)
    return words_by_cell


def _table_cells_by_origin(
    cells: list[TableCell],
) -> dict[tuple[int, int], TableCell]:
    """Index cells by origin while preserving the legacy first-match behavior."""

    cells_by_origin: dict[tuple[int, int], TableCell] = {}
    for cell in cells:
        cells_by_origin.setdefault((cell.row_start, cell.col_start), cell)
    return cells_by_origin


def _table_cell_order_index(
    table_cells: list[list[TableCell]],
) -> tuple[
    tuple[dict[tuple[int, int], list[TableCell]], list[TableCell]],
    dict[int, int],
]:
    coordinate_index = _table_cell_coordinate_index(table_cells)
    order = {
        id(cell): order_index
        for order_index, cell in enumerate(cell for row in table_cells for cell in row)
    }
    return coordinate_index, order


def _table_cells_ordered_at_center(
    center_x: float,
    center_y: float,
    index: tuple[
        tuple[dict[tuple[int, int], list[TableCell]], list[TableCell]],
        dict[int, int],
    ],
) -> list[TableCell]:
    coordinate_index, order = index
    candidates = list(_table_cells_at_center(center_x, center_y, coordinate_index))
    candidates.sort(key=lambda cell: order[id(cell)])
    return candidates


def _first_coordinate_word_match(
    target: PositionedWord,
    words: list[PositionedWord],
    index: dict[tuple[int, int], list[int]],
) -> int | None:
    """Find the first box within the same tolerance as the legacy scan."""

    left = float(target.left)
    top = float(target.top)
    tolerance = _COORDINATE_MATCH_TOLERANCE
    return min(
        (
            word_index
            for word_index in _coordinate_word_candidates(target, index)
            if (
                abs(float(words[word_index].left) - left) <= tolerance
                and abs(float(words[word_index].top) - top) <= tolerance
            )
        ),
        default=None,
    )


def _centered_table_cell_center(
    word: PositionedWord,
    index: tuple[dict[tuple[int, int], list[TableCell]], list[TableCell]],
) -> float | None:
    """Return the cell center when a source word is visibly centered in it."""

    center_x = float(word.left) + float(word.width) / 2.0
    center_y = float(word.top) + float(word.height) / 2.0
    for cell in _table_cells_at_center(center_x, center_y, index):
        cell_width = float(cell.right) - float(cell.left)
        if cell_width <= 0 or not (
            cell.left <= center_x <= cell.right
            and cell.top <= center_y <= cell.bottom
        ):
            continue
        if float(word.width) > cell_width * 1.05:
            return None
        left_margin = float(word.left) - float(cell.left)
        right_margin = float(cell.right) - (float(word.left) + float(word.width))
        if abs(left_margin - right_margin) <= max(8.0, cell_width * 0.15):
            return (float(cell.left) + float(cell.right)) / 2.0
        return None
    return None


def _fit_coordinate_word_edits(
    original_words: list[PositionedWord],
    edited_words: list[PositionedWord],
    table_cells: list[list[TableCell]],
    source_width: float = 0.0,
    source_height: float = 0.0,
    measured_widths: dict[int, float] | None = None,
) -> list[PositionedWord]:
    """Fit longer OCR edits without covering neighboring source content."""

    updated = [
        PositionedWord(
            word.text,
            word.left,
            word.top,
            word.width,
            word.height,
            is_form_field=word.is_form_field,
        )
        for word in edited_words
    ]
    updated_index = _coordinate_word_index(updated)
    centered_cell_index = _table_cell_coordinate_index(table_cells)
    for original_index, original in enumerate(original_words):
        index = _first_coordinate_word_match(original, updated, updated_index)
        if index is None:
            continue
        current = updated[index]
        original_text = clean_ocr_text(original.text)
        current_text = clean_ocr_text(current.text)
        if not original_text or current_text == original_text:
            continue
        length_ratio = max(len(current_text), 1) / max(len(original_text), 1)
        desired_width = max(
            original.width,
            min(original.width * 3.0, original.width * length_ratio * 1.1),
        )
        # The editor measures the actual browser text box after accounting for
        # the active font and wraps. Prefer that measurement when available;
        # older clients still submit no measurement, so keep the text-length
        # estimate as their fallback.
        measured_width = float((measured_widths or {}).get(original_index, 0.0))
        if isfinite(measured_width) and measured_width > original.width:
            desired_width = max(desired_width, measured_width)
        left_limit, right_limit = _coordinate_word_horizontal_bounds(
            original,
            updated,
            table_cells,
            source_width,
            index,
        )
        centered_x = _centered_table_cell_center(original, centered_cell_index)
        if centered_x is None:
            available_width = max(original.width, right_limit - original.left)
        else:
            center_available = 2.0 * min(
                max(centered_x - left_limit, 0.0),
                max(right_limit - centered_x, 0.0),
            )
            available_width = max(original.width, center_available)
        width = min(desired_width, available_width)
        left = current.left
        if centered_x is not None and width <= right_limit - left_limit:
            left = max(
                left_limit,
                min(right_limit - width, centered_x - width / 2.0),
            )
        candidate = PositionedWord(
            text=current.text,
            left=left,
            top=current.top,
            width=width,
            height=original.height,
            is_form_field=current.is_form_field,
        )
        _, bottom_limit = _coordinate_word_vertical_bounds(
            original,
            candidate,
            updated,
            table_cells,
            source_height,
            index,
        )
        available_height = max(original.height, bottom_limit - original.top)
        height = min(max(original.height, current.height), available_height)
        updated[index] = PositionedWord(
            text=current.text,
            left=left,
            top=current.top,
            width=width,
            height=height,
            is_form_field=current.is_form_field,
        )
    return updated


def _coordinate_word_horizontal_bounds(
    original: PositionedWord,
    positioned_words: list[PositionedWord],
    table_cells: list[list[TableCell]],
    source_width: float = 0.0,
    excluded_index: int | None = None,
) -> tuple[float, float]:
    """Return safe absolute horizontal bounds for an edited OCR word."""

    padding = max(1, min(3, int(round(original.height * 0.08))))
    left_limit = float(padding)
    right_limit = (
        float(source_width) - padding
        if source_width > padding
        else float("inf")
    )
    original_right = original.left + original.width

    for neighbor_index, neighbor in enumerate(positioned_words):
        if neighbor_index == excluded_index:
            continue
        vertical_overlap = min(
            original.top + original.height,
            neighbor.top + neighbor.height,
        ) - max(original.top, neighbor.top)
        shorter_height = max(min(original.height, neighbor.height), 1.0)
        if vertical_overlap / shorter_height < 0.35:
            continue
        neighbor_padding = max(1, min(3, int(round(neighbor.height * 0.08))))
        if neighbor.left >= original_right:
            right_limit = min(right_limit, neighbor.left - neighbor_padding - padding)
        elif neighbor.left + neighbor.width <= original.left:
            left_limit = max(
                left_limit,
                neighbor.left + neighbor.width + neighbor_padding + padding,
            )
        else:
            # OCR boxes can already overlap. Keep an edit inside its original
            # box in that case so the overlap never grows worse.
            left_limit = max(left_limit, original.left)
            right_limit = min(right_limit, original_right)

    for cells in table_cells:
        for cell in cells:
            if cell.right <= cell.left or cell.bottom <= cell.top:
                continue
            vertical_overlap = min(
                original.top + original.height,
                cell.bottom,
            ) - max(original.top, cell.top)
            shorter_height = max(min(original.height, cell.bottom - cell.top), 1.0)
            if vertical_overlap / shorter_height < 0.35:
                continue
            if cell.left >= original_right:
                right_limit = min(right_limit, cell.left - padding)
            elif cell.right <= original.left:
                left_limit = max(left_limit, cell.right + padding)
            elif cell.left <= original.left and cell.right >= original_right:
                # A heading inside a wide table cell may expand within that
                # cell, but must stop before its borders.
                left_limit = max(left_limit, cell.left + padding)
                right_limit = min(right_limit, cell.right - padding)
            else:
                left_limit = max(left_limit, original.left)
                right_limit = min(right_limit, original_right)

    return left_limit, right_limit


def _coordinate_word_vertical_bounds(
    original: PositionedWord,
    candidate: PositionedWord,
    positioned_words: list[PositionedWord],
    table_cells: list[list[TableCell]],
    source_height: float = 0.0,
    excluded_index: int | None = None,
) -> tuple[float, float]:
    """Return the safe vertical range for a wrapped edit using blank space only."""

    padding = max(1, min(3, int(round(original.height * 0.08))))
    top_limit = float(padding)
    bottom_limit = (
        float(source_height) - padding
        if source_height > padding
        else float("inf")
    )
    original_bottom = original.top + original.height
    candidate_right = candidate.left + candidate.width

    for neighbor_index, neighbor in enumerate(positioned_words):
        if neighbor_index == excluded_index:
            continue
        if min(candidate_right, neighbor.left + neighbor.width) <= max(candidate.left, neighbor.left):
            continue
        vertical_overlap = min(
            original_bottom,
            neighbor.top + neighbor.height,
        ) - max(original.top, neighbor.top)
        shorter_height = max(min(original.height, neighbor.height), 1.0)
        if vertical_overlap / shorter_height >= 0.35:
            continue
        neighbor_padding = max(1, min(3, int(round(neighbor.height * 0.08))))
        neighbor_bottom = neighbor.top + neighbor.height
        if neighbor.top >= original_bottom:
            bottom_limit = min(
                bottom_limit,
                neighbor.top - neighbor_padding - padding,
            )
        elif neighbor_bottom > original.top:
            # Existing vertical overlap is preserved but never enlarged.
            bottom_limit = min(bottom_limit, original_bottom)

    for cells in table_cells:
        for cell in cells:
            if cell.right <= cell.left or cell.bottom <= cell.top:
                continue
            if min(candidate_right, cell.right) <= max(candidate.left, cell.left):
                continue
            if cell.top >= original_bottom:
                bottom_limit = min(bottom_limit, cell.top - padding)
            elif cell.bottom > original.top:
                if cell.top <= original.top and cell.bottom >= original_bottom:
                    bottom_limit = min(bottom_limit, cell.bottom - padding)
                else:
                    bottom_limit = min(bottom_limit, original_bottom)

    return top_limit, bottom_limit


def create_docx(
    result: OcrResult,
    output_path: Path,
    source_image_path: Path | None = None,
    original_words: list | None = None,
    original_table_cells: list[list[TableCell]] | None = None,
    source_positioned_words: list[PositionedWord] | None = None,
    *,
    structured_table: bool = False,
    preserve_source_layout: bool = False,
    title: str = "扫描识别文档",
) -> None:
    # Default Word exports preserve the scan as a layout reference. The
    # structured_table path below deliberately skips that image-backed mode.
    has_ocr_content = bool(
        result.paragraphs
        or result.positioned_words
        or any(result.tables)
        or any(result.table_cells)
    )
    if (
        not structured_table
        and source_image_path
        and source_image_path.exists()
        and (
            preserve_source_layout
            or not has_ocr_content
            or result.tables
            or (original_words is not None and result.positioned_words)
        )
    ):
        _create_visual_editable_docx(
            result,
            output_path,
            source_image_path,
            original_words=original_words,
            original_table_cells=original_table_cells,
            source_positioned_words=source_positioned_words,
            title=title,
        )
        return

    document = Document()
    document.core_properties.title = title or "扫描识别文档"

    section = document.sections[0]
    edge_to_edge_table = bool(result.tables and _has_edge_to_edge_table(result))
    # 视频里的编辑页使用接近原图的窄页边距；较大的默认边距会把表格
    # 压窄，导致编号断行、列宽比例失真。
    # Full-width scan tables often begin close to the top edge as well. Match
    # that smaller top margin and reduce the following compensation together,
    # so both headings and the table stay near their measured source positions.
    native_heading_shift = (
        Pt(12.5)
        if edge_to_edge_table
        else Pt(16.25) if result.tables else Pt(0)
    )
    section.top_margin = (
        Cm(0.15) if edge_to_edge_table else Cm(0.58) if result.tables else Cm(1.15)
    )
    section.bottom_margin = Cm(0.15) if edge_to_edge_table else Cm(1.15)
    native_side_margin = _native_side_margin(result)
    section.left_margin = native_side_margin
    section.right_margin = native_side_margin
    landscape_table = bool(
        result.tables
        and result.image_width > 0
        and result.image_height > 0
        and result.image_width > result.image_height
    )
    section.orientation = (
        WD_ORIENT.LANDSCAPE if landscape_table else WD_ORIENT.PORTRAIT
    )
    page_width_pt = A4_LONG_SIDE_PT if landscape_table else A4_SHORT_SIDE_PT
    # Match the page orientation to the source scan so landscape forms do not
    # lose nearly a third of their usable width in the editable Word export.
    section.page_width = Pt(page_width_pt)
    if result.tables and result.image_width > 0 and result.image_height > 0:
        # A photographed form is often not A4-shaped after perspective correction.
        # Keeping the page aspect ratio prevents the native editable version from
        # gaining a large blank tail or stretching the table vertically.
        section.page_height = Pt(page_width_pt * result.image_height / result.image_width)
        if edge_to_edge_table:
            # Keep the page almost at the source aspect ratio while accounting
            # for Word's title/table flow rounding at the bottom edge.
            section.page_height += Pt(8)
        # Other tables keep the source page height. If OCR text needs more room,
        # measured row-span expansion below adds only the required height.
    else:
        section.page_height = Cm(29.7)
    normal_style = document.styles["Normal"]
    normal_style.font.name = "Microsoft YaHei"
    normal_style.font.size = Pt(11)
    normal_style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

    paragraph_blocks = _paragraph_blocks(result)
    if result.tables and result.positioned_words:
        paragraph_blocks = [
            (
                text,
                style,
                layout
                or (
                    _positioned_paragraph_layout(result, text)
                    if style == "body"
                    else None
                ),
            )
            for text, style, layout in paragraph_blocks
        ]
    # A plain prose scan should export as normal editable paragraphs. Keep the
    # original heading metadata available to the visual-image editor, where it
    # is used only for positioning changed text, but do not let it turn the
    # first OCR lines into a centered/bold Word title.
    if not result.tables:
        paragraph_blocks = [
            (text, "body" if style in {"title", "subtitle"} else style, layout)
            for text, style, layout in paragraph_blocks
        ]
    clean_paragraph_text = clean_ocr_text if result.tables else clean_plain_ocr_text
    flow_blocks: list[tuple[float, int, object]] = []
    flow_block_order = 0
    first_table_top = min(
        (
            cell.top
            for cells in result.table_cells
            for cell in cells
            if cell.right > cell.left and cell.bottom > cell.top
        ),
        default=float("inf"),
    )
    native_heading_shift_applied = False
    for paragraph_index, (paragraph, paragraph_style, paragraph_layout) in enumerate(paragraph_blocks):
        cleaned = clean_paragraph_text(paragraph)
        if cleaned:
            output_paragraph = document.add_paragraph(style="Normal")
            paragraph_order = (
                float(paragraph_layout.top)
                if result.tables and paragraph_layout is not None
                else float("-inf")
            )
            flow_blocks.append((paragraph_order, flow_block_order, output_paragraph._p))
            flow_block_order += 1
            is_heading = paragraph_style in {"title", "subtitle"}
            output_paragraph.alignment = (
                WD_ALIGN_PARAGRAPH.CENTER if is_heading else WD_ALIGN_PARAGRAPH.LEFT
            )
            # Positioned OCR fragments preserve horizontal gaps and fill-in
            # fields, but Word still lays the paragraph out in document flow;
            # it does not place the line at its source-image y-coordinate.
            # The margin compensation is a document-level offset. Applying it
            # to every header line accumulates extra height and can push the
            # bottom rows of a source-sized form onto a second page.
            needs_native_heading_shift = (
                result.tables
                and not is_heading
                and (
                    paragraph_layout is None
                    or paragraph_layout.top < first_table_top
                )
            )
            output_paragraph.paragraph_format.space_before = (
                native_heading_shift
                if needs_native_heading_shift and not native_heading_shift_applied
                else Pt(0)
            )
            if needs_native_heading_shift:
                native_heading_shift_applied = True
            # The source form's header line touches the table after a small
            # gap. A generic body paragraph-after of 6pt adds a visible blank
            # band in native table exports, while text-only documents still
            # need their normal paragraph separation.
            output_paragraph.paragraph_format.space_after = Pt(
                0 if is_heading or result.tables else 6
            )
            if (
                result.tables
                and paragraph_style == "title"
                and paragraph_index + 1 < len(paragraph_blocks)
                and paragraph_blocks[paragraph_index + 1][1] == "subtitle"
            ):
                output_paragraph.paragraph_format.space_after = Pt(
                    _ocr_heading_gap_pt(
                        result,
                        cleaned,
                        paragraph_blocks[paragraph_index + 1][0],
                        section.page_width.pt,
                    )
                )
            output_paragraph.paragraph_format.line_spacing = 1.0 if is_heading else 1.4
            output_paragraph.paragraph_format.keep_with_next = is_heading
            # Fragment coordinates are useful for form labels and blank
            # fields, but using them for a normal prose scan turns ordinary
            # word gaps into artificial underline fields. Plain-text output
            # should remain a normal editable paragraph flow.
            if (
                result.tables
                and paragraph_layout
                and paragraph_layout.fragments
                and paragraph_style == "body"
            ):
                _add_positioned_line(
                    output_paragraph,
                    paragraph_layout,
                    result.positioned_words,
                    native_side_margin.pt,
                    page_width_pt=section.page_width.pt,
                )
            else:
                run = output_paragraph.add_run(cleaned)
                heading_font = "SimSun" if result.tables else "Microsoft YaHei"
                run.font.name = heading_font
                if paragraph_style == "title":
                    run.font.size = Pt(
                        _ocr_heading_font_size_pt(
                            result,
                            cleaned,
                            section.page_width.pt,
                            scale=0.88,
                        )
                        or 13.5
                    )
                    run.font.bold = not bool(result.tables)
                elif paragraph_style == "subtitle":
                    run.font.size = Pt(
                        _ocr_heading_font_size_pt(
                            result,
                            cleaned,
                            section.page_width.pt,
                            scale=0.88,
                        )
                        or 12.5
                    )
                    run.font.bold = not bool(result.tables)
                else:
                    run.font.size = Pt(11)
                    run.font.bold = False
                run_fonts = run._element.get_or_add_rPr().rFonts
                for font_key in ("ascii", "hAnsi", "eastAsia", "cs"):
                    run_fonts.set(qn(f"w:{font_key}"), heading_font)

    # Competition-result pages may have only title/subtitle paragraphs before
    # the table. Add the same compensation there so their table top does not
    # move when the heading margin is corrected.
    if result.tables and not any(style == "body" for _, style, _ in paragraph_blocks):
        spacer = document.add_paragraph()
        spacer.paragraph_format.space_before = native_heading_shift
        spacer.paragraph_format.space_after = Pt(0)
        spacer.paragraph_format.line_spacing = 0.01
        flow_blocks.append((first_table_top - 0.01, flow_block_order, spacer._p))
        flow_block_order += 1

    cleaned_text_by_word_id: dict[int, str] = {}
    positioned_words_by_cell = _positioned_words_by_table_cell(
        result.table_cells,
        result.positioned_words,
        cleaned_text_by_word_id,
    )
    checkbox_source_width = max(result.image_width, 0)
    checkbox_page_scale = page_width_pt / max(checkbox_source_width, 1)
    checkbox_boxes: list[tuple[float, float, float, float]] = []
    checkbox_states: dict[tuple[int, int], bool] = {}
    native_row_expansion_pt = 0.0
    if source_image_path is not None and source_image_path.exists():
        try:
            with Image.open(source_image_path) as source_image:
                checkbox_source_width = source_image.width
        except OSError:
            # OCR geometry still supports a native table when a legacy source
            # image is unavailable or cannot be decoded.
            checkbox_boxes = []
        else:
            checkbox_page_scale = page_width_pt / max(checkbox_source_width, 1)
            checkbox_boxes = _source_checkbox_boxes(source_image_path)
            checkbox_states = _source_checkbox_states(source_image_path, checkbox_boxes)
    for table_index, table_data in enumerate(result.tables):
        if not table_data:
            continue
        table_cells = (
            result.table_cells[table_index]
            if table_index < len(result.table_cells)
            else []
        )
        cells_by_origin = _table_cells_by_origin(table_cells)
        original_cells_by_origin = (
            _table_cells_by_origin(
                original_table_cells[table_index]
                if table_index < len(original_table_cells)
                else []
            )
            if original_table_cells is not None
            else None
        )
        column_count = max(len(row) for row in table_data)
        table = document.add_table(rows=len(table_data), cols=column_count)
        table_order = min(
            (
                cell.top
                for cell in table_cells
                if cell.right > cell.left and cell.bottom > cell.top
            ),
            default=float("inf"),
        )
        table.style = "Table Grid"
        table.autofit = False
        layout = result.table_layouts[table_index] if table_index < len(result.table_layouts) else TableLayout()
        usable_width = section.page_width - section.left_margin - section.right_margin
        if layout and layout.source_width > 0 and layout.source_right > layout.source_left:
            # Layout coordinates are measured from the full source image, not from
            # the text area inside Word's margins. Map them to the full page first,
            # then convert the left offset to a table indent relative to the text
            # area. Using usable_width here shrinks wide forms and shifts them.
            table_width = min(
                _table_target_width(layout, section.page_width),
                usable_width,
            )
            target_left = _table_target_left(layout, section.page_width)
            centered_table_left = section.left_margin + max(
                usable_width - table_width,
                0,
            ) / 2
            # Word and LibreOffice place the visible table border slightly to
            # the left of its computed indent. Correct only off-center scans,
            # and keep the adjustment small so centered tables stay in place.
            alignment_delta = target_left - centered_table_left
            border_inset_adjustment = Pt(0)
            if alignment_delta > Pt(0.5):
                border_inset_adjustment = Pt(1)
            elif alignment_delta < Pt(-0.5):
                border_inset_adjustment = Pt(-1)
            alignment_correction = min(
                Pt(4),
                max(
                    Pt(-4),
                    alignment_delta + border_inset_adjustment,
                ),
            )
            table_indent = min(
                max(
                    target_left - section.left_margin + alignment_correction,
                    0,
                ),
                max(usable_width - table_width, 0),
            )
        else:
            table_width = usable_width
            table_indent = 0
        column_widths = _column_widths(layout, column_count, table_width)
        source_table_extent = layout.source_right - layout.source_left
        table_source_width = (
            source_table_extent
            if isfinite(source_table_extent) and source_table_extent > 0
            else layout.source_width or result.image_width or 1.0
        )
        if not isfinite(table_source_width) or table_source_width <= 0:
            table_source_width = float(max(result.image_width, 1))
        table_scale = table_width / EMU_PER_PT / max(table_source_width, 1.0)
        layout_source_width = 0.0
        for raw_width in layout.column_widths:
            try:
                source_column_width = float(raw_width)
            except (TypeError, ValueError, OverflowError):
                continue
            if isfinite(source_column_width) and source_column_width > 0:
                layout_source_width += source_column_width
        native_row_heights_pt: list[float] = []
        for row_index in range(len(table_data)):
            try:
                source_height = float(layout.row_heights[row_index])
            except (IndexError, TypeError, ValueError, OverflowError):
                source_height = -1.0
            if (
                row_index < len(layout.row_heights)
                and layout_source_width > 0
                and isfinite(source_height)
                and source_height >= 0
            ):
                source_height = max(source_height, 1.0)
                height_emu = table_width * source_height / layout_source_width
                if isfinite(height_emu):
                    native_row_heights_pt.append(
                        max(int(height_emu), Cm(0.35)) / EMU_PER_PT
                    )
                else:
                    native_row_heights_pt.append(0.0)
            else:
                native_row_heights_pt.append(0.0)
        cell_height_requirements: dict[tuple[int, int], float] = {}

        def record_cell_height_requirement(
            text: str,
            source_cell: TableCell | None,
            source_words: Iterable[PositionedWord],
            row_index: int,
            column_index: int,
            font_size: float,
            checkbox_layout: _NativeCheckboxLayout | None,
        ) -> None:
            if source_cell:
                row_start = min(max(source_cell.row_start, 0), len(native_row_heights_pt))
                row_end = min(max(source_cell.row_end, 0), len(native_row_heights_pt))
                col_start = min(max(source_cell.col_start, 0), len(column_widths))
                col_end = min(max(source_cell.col_end, 0), len(column_widths))
            else:
                row_start = max(row_index, 0)
                row_end = min(row_index + 1, len(native_row_heights_pt))
                col_start = max(column_index, 0)
                col_end = min(column_index + 1, len(column_widths))
            available_height = sum(native_row_heights_pt[row_start:row_end])
            cell_width_pt = sum(column_widths[col_start:col_end]) / EMU_PER_PT
            if available_height <= 0 or cell_width_pt <= 0:
                return
            if _is_vertical_section_label(source_cell, text, source_words):
                return
            if checkbox_layout:
                required_height = 1.0 + sum(
                    checkbox_layout.footer_space_after_pt
                    if not line
                    else checkbox_layout.line_spacing_pt
                    for line in checkbox_layout.paragraph_texts
                )
            else:
                required_height = _native_cell_required_height_pt(
                    text,
                    cell_width_pt,
                    font_size,
                )
            span = (row_start, row_end)
            cell_height_requirements[span] = max(
                cell_height_requirements.get(span, 0.0),
                required_height,
            )

        for column_index, width in enumerate(column_widths):
            table.columns[column_index].width = width
            for cell in table.columns[column_index].cells:
                cell.width = width
        _set_table_width(table, table_width, table_indent)
        _set_cell_margins(table, top=0, start=45, bottom=0, end=45)

        for row_index, row in enumerate(table_data):
            if native_row_heights_pt[row_index] > 0:
                table.rows[row_index].height = int(
                    round(native_row_heights_pt[row_index] * EMU_PER_PT)
                )
                # The line detector already measured the source row height.
                # AT_LEAST lets Word/LibreOffice expand rows for text and the
                # error accumulates down long forms, moving the bottom far
                # away from the scan. Keep the detected geometry fixed.
                table.rows[row_index].height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
            for column_index, value in enumerate(row):
                cell = table.cell(row_index, column_index)
                source_cell = cells_by_origin.get((row_index, column_index))
                cell_text = _native_cell_text(
                    result,
                    source_cell,
                    value,
                    positioned_words_by_cell.get(id(source_cell), ()) if source_cell else (),
                    cleaned_text_by_word_id,
                )
                checkbox_layout = _layout_native_checkbox_text(
                    cell_text,
                    source_cell,
                    checkbox_boxes,
                    checkbox_source_width,
                    checkbox_page_scale,
                    _native_checkbox_states_for_cell(
                        checkbox_states,
                        source_cell.text if source_cell else cell_text,
                        source_cell,
                        original_cells_by_origin,
                    ),
                    positioned_words=(
                        positioned_words_by_cell.get(id(source_cell), ())
                        if source_cell
                        else ()
                    ),
                )
                cell_words = (
                    positioned_words_by_cell.get(id(source_cell), ())
                    if source_cell
                    else ()
                )
                cell.text = checkbox_layout.text if checkbox_layout else cell_text
                cell.vertical_alignment = _native_cell_vertical_alignment(
                    source_cell,
                    cell_words,
                )
                font_size = (
                    checkbox_layout.font_size_pt
                    if checkbox_layout
                    else _native_cell_font_size_for_geometry(
                        cell_text,
                        source_cell,
                        row_index,
                        column_index,
                        column_widths,
                        native_row_heights_pt,
                        table_scale,
                        [word.height * table_scale for word in cell_words],
                        [word.width * table_scale for word in cell_words],
                    )
                )
                vertical_label_line_spacing_pt = (
                    _native_vertical_section_label_line_spacing_pt(
                        source_cell,
                        cell.text,
                        cell_words,
                        table_scale,
                    )
                )
                _set_vertical_section_label(
                    cell,
                    source_cell,
                    cell.text,
                    cell_words,
                )
                _style_cell(
                    cell,
                    False,
                    font_size=font_size,
                    horizontal_alignment=(
                        WD_ALIGN_PARAGRAPH.LEFT
                        if checkbox_layout
                        else _native_cell_horizontal_alignment(
                            source_cell,
                            cell_words,
                            cell_text,
                        )
                    ),
                )
                _apply_vertical_section_label_line_spacing(
                    cell,
                    vertical_label_line_spacing_pt,
                )
                if checkbox_layout:
                    _apply_native_checkbox_layout(cell, checkbox_layout)
                record_cell_height_requirement(
                    cell_text,
                    source_cell,
                    cell_words,
                    row_index,
                    column_index,
                    font_size,
                    checkbox_layout,
                )

        # Table OCR also reports merged-cell coordinates. Applying those spans
        # keeps forms from being flattened into a misleading uniform grid.
        if table_index < len(result.table_cells):
            merge_plan = _native_merge_plan(
                table_cells,
                len(table_data),
                column_count,
            )
            for source_cell in merge_plan:
                merge_span = _native_merge_span(
                    source_cell,
                    len(table_data),
                    column_count,
                )
                if merge_span is None:
                    continue
                row_start, row_end, col_start, col_end = merge_span
                merged = table.cell(
                    row_start,
                    col_start,
                ).merge(
                    table.cell(row_end - 1, col_end - 1),
                )
                source_cell_text = _native_cell_text(
                    result,
                    source_cell,
                    source_cell.text,
                    positioned_words_by_cell.get(id(source_cell), ()),
                    cleaned_text_by_word_id,
                )
                cell_text = _preserve_native_merged_text(merged.text, source_cell_text)
                checkbox_layout = _layout_native_checkbox_text(
                    cell_text,
                    source_cell,
                    checkbox_boxes,
                    checkbox_source_width,
                    checkbox_page_scale,
                    _native_checkbox_states_for_cell(
                        checkbox_states,
                        source_cell.text if source_cell else cell_text,
                        source_cell,
                        original_cells_by_origin,
                    ),
                    positioned_words=positioned_words_by_cell.get(id(source_cell), ()),
                )
                source_cell_words = positioned_words_by_cell.get(id(source_cell), ())
                merged.text = checkbox_layout.text if checkbox_layout else cell_text
                merged.vertical_alignment = _native_cell_vertical_alignment(
                    source_cell,
                    source_cell_words,
                )
                font_size = (
                    checkbox_layout.font_size_pt
                    if checkbox_layout
                    else _native_cell_font_size_for_geometry(
                        cell_text,
                        source_cell,
                        source_cell.row_start,
                        source_cell.col_start,
                        column_widths,
                        native_row_heights_pt,
                        table_scale,
                        [word.height * table_scale for word in source_cell_words],
                        [word.width * table_scale for word in source_cell_words],
                    )
                )
                vertical_label_line_spacing_pt = (
                    _native_vertical_section_label_line_spacing_pt(
                        source_cell,
                        merged.text,
                        source_cell_words,
                        table_scale,
                    )
                )
                _set_vertical_section_label(
                    merged,
                    source_cell,
                    merged.text,
                    source_cell_words,
                )
                _style_cell(
                    merged,
                    False,
                    font_size=font_size,
                    horizontal_alignment=(
                        WD_ALIGN_PARAGRAPH.LEFT
                        if checkbox_layout
                        else _native_cell_horizontal_alignment(
                            source_cell,
                            source_cell_words,
                            cell_text,
                        )
                    ),
                )
                _apply_vertical_section_label_line_spacing(
                    merged,
                    vertical_label_line_spacing_pt,
                )
                if checkbox_layout:
                    _apply_native_checkbox_layout(merged, checkbox_layout)
                record_cell_height_requirement(
                    cell_text,
                    source_cell,
                    source_cell_words,
                    source_cell.row_start,
                    source_cell.col_start,
                    font_size,
                    checkbox_layout,
                )
        # Preserve OCR row heights unless text still overflows at the minimum
        # readable size; then expand only the affected row spans.
        for (row_start, row_end), required_height in sorted(
            cell_height_requirements.items(),
            key=lambda item: item[0][1] - item[0][0],
        ):
            available_height = sum(native_row_heights_pt[row_start:row_end])
            if required_height <= available_height:
                continue
            expansion = required_height - available_height
            native_row_heights_pt[row_end - 1] += expansion
            native_row_expansion_pt += expansion
        for row_index, height_pt in enumerate(native_row_heights_pt):
            if height_pt > 0:
                table.rows[row_index].height = int(round(height_pt * EMU_PER_PT))
        flow_blocks.append((table_order, flow_block_order, table._tbl))
        flow_block_order += 1

    if native_row_expansion_pt > 0:
        section.page_height = section.page_height + Pt(native_row_expansion_pt)
    if not paragraph_blocks and not result.tables:
        document.add_paragraph("未识别到文字。")
    if result.tables and flow_blocks:
        body = document._element.body
        ordered_blocks = sorted(flow_blocks, key=lambda block: (block[0], block[1]))
        for _top, _sequence, element in ordered_blocks:
            if element.getparent() is body:
                body.remove(element)
        for index, (_top, _sequence, element) in enumerate(ordered_blocks):
            body.insert(index, element)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)


def create_native_table_docx(
    result: OcrResult,
    output_path: Path,
    source_image_path: Path | None = None,
    *,
    original_table_cells: list[list[TableCell]] | None = None,
    title: str = "扫描识别文档",
) -> None:
    """Create editable OCR text and native Word table cells without a scan layer."""

    create_docx(
        result,
        output_path,
        source_image_path,
        original_table_cells=original_table_cells,
        structured_table=True,
        title=title,
    )


_INLINE_MARKDOWN = re.compile(
    r"(\*\*.+?\*\*|__.+?__|~~.+?~~|(?P<code_ticks>`+)[\s\S]+?(?P=code_ticks)|\\[\s\S]|\[[^\]]+\]\([^)]+\)|(?<!\*)\*(?!\s).+?(?<!\s)\*(?!\*)|(?<![\w])_(?!\s).+?(?<!\s)_(?![\w]))"
)


def _add_markdown_runs(paragraph, text: str) -> None:
    """Render common inline Markdown marks as native Word run formatting."""
    cursor = 0
    for match in _INLINE_MARKDOWN.finditer(text):
        if match.start() > cursor:
            paragraph.add_run(text[cursor:match.start()])
        token = match.group(0)
        if token.startswith(("**", "__")):
            run = paragraph.add_run(token[2:-2])
            run.bold = True
        elif token.startswith("~~"):
            run = paragraph.add_run(token[2:-2])
            run.font.strike = True
        elif token.startswith(("*", "_")):
            run = paragraph.add_run(token[1:-1])
            run.italic = True
        elif token.startswith("`"):
            delimiter_length = len(token) - len(token.lstrip("`"))
            code_text = token[delimiter_length:-delimiter_length]
            code_text = (
                code_text.replace("\r\n", " ")
                .replace("\r", " ")
                .replace("\n", " ")
            )
            if (
                code_text.startswith(" ")
                and code_text.endswith(" ")
                and code_text.strip(" ")
            ):
                code_text = code_text[1:-1]
            run = paragraph.add_run(code_text)
            run.font.name = "Consolas"
        elif token.startswith("\\") and len(token) > 1:
            escaped = token[1:]
            if escaped.isascii() and not escaped.isalnum() and not escaped.isspace():
                paragraph.add_run(escaped)
            else:
                paragraph.add_run(token)
        else:
            link = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", token)
            if link:
                _add_markdown_hyperlink(paragraph, link.group(1), link.group(2))
            else:
                paragraph.add_run(token)
        cursor = match.end()
    if cursor < len(text):
        paragraph.add_run(text[cursor:])


def _add_markdown_hyperlink(paragraph, label: str, target: str) -> None:
    """Create a clickable Word relationship for supported external links."""

    destination = target.strip()
    if any(ord(character) < 0x20 for character in destination):
        paragraph.add_run(label)
        return
    try:
        parsed = urlsplit(destination)
    except ValueError:
        paragraph.add_run(label)
        return
    if parsed.scheme.lower() not in {"http", "https", "mailto"}:
        paragraph.add_run(label)
        return
    if parsed.scheme.lower() in {"http", "https"} and not parsed.netloc:
        paragraph.add_run(label)
        return

    relationship_id = paragraph.part.relate_to(
        destination,
        RELATIONSHIP_TYPE.HYPERLINK,
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)
    run = OxmlElement("w:r")
    run_properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    run_properties.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    run_properties.append(underline)
    run.append(run_properties)
    visible_text = OxmlElement("w:t")
    visible_text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    visible_text.text = label
    run.append(visible_text)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _markdown_table_cells(line: str) -> list[str]:
    """Split a Markdown table row without splitting escaped or code-span pipes."""
    value = line.strip()
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    code_ticks = 0
    index = 0
    has_leading_delimiter = value.startswith("|")
    has_trailing_delimiter = False
    while index < len(value):
        character = value[index]
        if escaped:
            current.append(character)
            escaped = False
            has_trailing_delimiter = False
        elif character == "\\" and code_ticks:
            current.append(character)
            has_trailing_delimiter = False
        elif character == "\\":
            next_character = value[index + 1] if index + 1 < len(value) else ""
            if next_character in {"|", "\\"}:
                escaped = True
            else:
                current.append(character)
            has_trailing_delimiter = False
        elif character == "`":
            run_end = index + 1
            while run_end < len(value) and value[run_end] == "`":
                run_end += 1
            tick_count = run_end - index
            code_ticks = 0 if code_ticks == tick_count else tick_count if not code_ticks else code_ticks
            current.extend(value[index:run_end])
            has_trailing_delimiter = False
            index = run_end - 1
        elif character == "|" and not code_ticks:
            cells.append("".join(current).strip())
            current = []
            has_trailing_delimiter = True
        else:
            current.append(character)
            has_trailing_delimiter = False
        index += 1
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    if has_leading_delimiter and cells:
        cells.pop(0)
    if has_trailing_delimiter and cells:
        cells.pop()
    return cells


def _markdown_table_alignment(line: str) -> list[str] | None:
    cells = _markdown_table_cells(line)
    if not cells or not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
        return None
    alignments = []
    for cell in cells:
        left = cell.startswith(":")
        right = cell.endswith(":")
        alignments.append("center" if left and right else "left" if left else "right" if right else "left")
    return alignments


def _markdown_table_start(
    lines: list[str],
    index: int,
) -> tuple[list[str], list[str], int] | None:
    """Return a table header, alignments, and separator index if present."""
    if index >= len(lines) or "|" not in lines[index]:
        return None
    header = _markdown_table_cells(lines[index])
    if not header:
        return None
    separator_index = index + 1
    while (
        separator_index < len(lines)
        and not lines[separator_index].strip()
        and separator_index - index <= 3
    ):
        separator_index += 1
    if separator_index >= len(lines) or "|" not in lines[separator_index]:
        return None
    alignments = _markdown_table_alignment(lines[separator_index])
    if alignments is None or len(header) != len(alignments):
        return None
    return header, alignments, separator_index


def _markdown_prose_line(line: str) -> tuple[str, bool]:
    """Remove Markdown hard-break markers and report whether the line breaks."""
    trimmed = line.rstrip()
    trailing_backslashes = len(trimmed) - len(trimmed.rstrip("\\"))
    if trailing_backslashes % 2:
        return trimmed[:-1].rstrip(), True
    trailing_spaces = len(line) - len(line.rstrip(" "))
    if trailing_spaces >= 2:
        return line[:-trailing_spaces], True
    return trimmed, False


def _markdown_fence(line: str) -> tuple[str, int, str] | None:
    """Parse a fenced-code marker while keeping its character and length."""
    match = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
    if not match:
        return None
    marker, info = match.groups()
    if marker.startswith("`") and "`" in info:
        return None
    return marker[0], len(marker), info.strip()


def _add_markdown_table(document, header: list[str], alignments: list[str], rows: list[list[str]]) -> None:
    column_count = len(header)
    table = document.add_table(rows=1, cols=column_count)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    for row_index, values in enumerate([header, *rows]):
        cells = table.rows[0].cells if row_index == 0 else table.add_row().cells
        for column_index, cell in enumerate(cells):
            value = values[column_index] if column_index < len(values) else ""
            paragraph = cell.paragraphs[0]
            paragraph.alignment = {
                "center": WD_ALIGN_PARAGRAPH.CENTER,
                "right": WD_ALIGN_PARAGRAPH.RIGHT,
            }.get(alignments[column_index], WD_ALIGN_PARAGRAPH.LEFT)
            _add_markdown_runs(paragraph, value)
            if row_index == 0:
                for run in paragraph.runs:
                    run.bold = True
                properties = cell._tc.get_or_add_tcPr()
                shading = OxmlElement("w:shd")
                shading.set(qn("w:fill"), "EAF1FB")
                properties.append(shading)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    _repeat_table_header(table.rows[0])


def create_assistant_docx(title: str, content: str, output_path: Path) -> None:
    """Create an editable Word document with Markdown headings, lists and tables."""
    document = Document()
    document.core_properties.title = title or "未命名文档"
    normal_style = document.styles["Normal"]
    normal_style.font.name = "Microsoft YaHei"
    normal_style.font.size = Pt(11)
    normal_rpr = normal_style.element.get_or_add_rPr()
    normal_fonts = normal_rpr.rFonts
    if normal_fonts is None:
        normal_fonts = OxmlElement("w:rFonts")
        normal_rpr.append(normal_fonts)
    normal_fonts.set(qn("w:eastAsia"), "Microsoft YaHei")

    document.add_heading(title or "未命名文档", level=0)
    lines = content.splitlines()
    code_fence: tuple[str, int] | None = None
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.rstrip()
        fence = _markdown_fence(line)
        if code_fence is not None:
            if (
                fence is not None
                and fence[0] == code_fence[0]
                and fence[1] >= code_fence[1]
                and not fence[2]
            ):
                code_fence = None
                index += 1
                continue
            paragraph = document.add_paragraph(raw_line, style="No Spacing")
            for run in paragraph.runs:
                run.font.name = "Consolas"
            index += 1
            continue
        if fence is not None:
            code_fence = (fence[0], fence[1])
            index += 1
            continue
        if not line.strip():
            index += 1
            continue

        table_start = _markdown_table_start(lines, index)
        if table_start is not None:
            header, alignments, separator_index = table_start
            rows = []
            index = separator_index + 1
            while index < len(lines):
                if not lines[index].strip():
                    next_row = index
                    while next_row < len(lines) and not lines[next_row].strip() and next_row - index <= 3:
                        next_row += 1
                    if next_row < len(lines) and "|" in lines[next_row]:
                        if _markdown_table_start(lines, next_row) is not None:
                            break
                        candidate = _markdown_table_cells(lines[next_row])
                        if len(candidate) == len(header):
                            index = next_row
                            continue
                    break
                row = _markdown_table_cells(lines[index])
                if len(row) == len(header):
                    rows.append(row)
                else:
                    break
                index += 1
            _add_markdown_table(document, header, alignments, rows)
            continue

        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        list_item = re.match(r"^(\s*)([-+*]|\d+[.)])\s+(.+)$", line)
        if heading:
            paragraph = document.add_heading(level=len(heading.group(1)))
            _add_markdown_runs(paragraph, heading.group(2))
        elif re.fullmatch(r"\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*", line):
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_before = Pt(4)
            paragraph.paragraph_format.space_after = Pt(4)
            p_pr = paragraph._p.get_or_add_pPr()
            borders = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "4")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "C8CED8")
            borders.append(bottom)
            p_pr.append(borders)
        elif list_item:
            marker = list_item.group(2)
            style = "List Number" if marker[0].isdigit() else "List Bullet"
            paragraph = document.add_paragraph(style=style)
            indentation = len(list_item.group(1).replace("\t", "    "))
            paragraph.paragraph_format.left_indent = Cm(0.63 * (indentation // 2))
            _add_markdown_runs(paragraph, list_item.group(3))
        elif line.lstrip().startswith(">"):
            paragraph = document.add_paragraph(style="Quote")
            _add_markdown_runs(paragraph, re.sub(r"^\s*>\s?", "", line))
        else:
            paragraph = document.add_paragraph()
            first_line, first_hard_break = _markdown_prose_line(lines[index])
            paragraph_lines = [first_line.strip()]
            hard_breaks = [first_hard_break]
            next_index = index + 1
            while next_index < len(lines):
                candidate_source = lines[next_index]
                candidate = candidate_source.rstrip()
                candidate_text = candidate.strip()
                if not candidate_text:
                    break
                if (
                    _markdown_fence(candidate) is not None
                    or _markdown_table_start(lines, next_index) is not None
                    or re.match(r"^\s{0,3}#{1,6}\s+", candidate)
                    or re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", candidate)
                    or re.fullmatch(r"\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*", candidate)
                    or candidate.lstrip().startswith(">")
                ):
                    break
                prose_line, hard_break = _markdown_prose_line(candidate_source)
                paragraph_lines.append(prose_line.strip())
                hard_breaks.append(hard_break)
                next_index += 1
            paragraph_text = paragraph_lines[0]
            for line_index, continuation in enumerate(paragraph_lines[1:]):
                separator = "\n" if hard_breaks[line_index] else " "
                paragraph_text += separator + continuation
            _add_markdown_runs(paragraph, paragraph_text)
            index = next_index
            continue
        index += 1
    document.save(output_path)
    try:
        # 重新打开 ZIP/XML 结构，避免把损坏文件返回给手机后才由 Word 报错。
        Document(output_path)
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("生成的 Word 文档校验失败") from exc


def _create_visual_editable_docx(
    result: OcrResult,
    output_path: Path,
    source_image_path: Path,
    original_words: list | None = None,
    original_table_cells: list[list[TableCell]] | None = None,
    source_positioned_words: list[PositionedWord] | None = None,
    title: str = "扫描识别文档",
) -> None:
    """Keep the scanned page intact and overlay boxes only after an edit.

    Adding transparent DrawingML text boxes to an untouched scan makes
    LibreOffice/WPS render OCR text a second time. The fidelity copy therefore
    keeps the original page image until the user changes content, then places
    editable Word textboxes over only the changed OCR regions.
    """

    if original_words is None:
        _create_image_only_docx(result, output_path, source_image_path, title=title)
        return

    rendered_background: Path | None = None
    background_path = source_image_path
    # The scan already contains the printed OCR text. Always erase the current
    # and (after an edit) original OCR rectangles first, then draw one editable
    # text-box layer on top. Baking edited text into the original image and also
    # drawing all editable boxes produces visible double glyphs in LibreOffice
    # and WPS; transparent shape fill does not hide the text itself.
    with tempfile.NamedTemporaryFile(
        prefix="docx_background_",
        suffix=".png",
        dir=source_image_path.parent,
        delete=False,
    ) as temporary:
        rendered_background = Path(temporary.name)
    visual_words = list(result.positioned_words)
    # Some table OCR providers return complete cell text but omit the same
    # text from the positioned-word stream. Only synthesize boxes for cells
    # whose text differs from the normalized source cell. Adding every such
    # cell after an edit made unchanged scan text look like a user edit and
    # drew a second OCR layer over the original page.
    visual_words.extend(
        _missing_table_cell_words(
            result,
            visual_words,
            original_table_cells,
            source_positioned_words,
        )
    )
    visual_words.extend(_empty_table_cell_words(result, visual_words))
    source_word_index = _build_source_word_spatial_index(original_words)
    original_coordinate_index = _coordinate_word_index(original_words)
    unchanged_words = [
        _has_same_positioned_word(word, original_words, original_coordinate_index)
        for word in visual_words
    ]
    erase_words, changed_cells = _visual_edit_regions(
        result,
        visual_words,
        original_words,
    )
    _erase_positioned_text(
        result,
        source_image_path,
        rendered_background,
        extra_words=original_words,
        words_to_erase=erase_words,
        clear_cells=changed_cells,
    )
    background_path = rendered_background

    with Image.open(background_path) as image:
        source_width, source_height = image.size

    page_width_pt, page_height_pt = _image_page_size(source_width, source_height)
    page_scale = page_width_pt / max(source_width, 1)

    document = Document()
    document.core_properties.title = title or "扫描识别文档"
    section = document.sections[0]
    _set_image_page_size(section, page_width_pt, page_height_pt)
    section.top_margin = Pt(0)
    section.bottom_margin = Pt(0)
    section.left_margin = Pt(0)
    section.right_margin = Pt(0)
    section.header_distance = Pt(0)
    section.footer_distance = Pt(0)

    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 0.01
    _append_drawing_image(paragraph, background_path, page_width_pt, page_height_pt)

    centered_texts = _centered_paragraph_texts(result)
    table_cell_order_index = _table_cell_order_index(result.table_cells)
    source_ink_colors: dict[int, tuple[int, int, int]] = {}
    with Image.open(source_image_path) as ink_source:
        for index, word in enumerate(visual_words):
            is_edited = not unchanged_words[index]
            if is_edited and clean_ocr_text(_native_word_text(word)):
                source_ink_colors[index] = _source_ink_rgb(
                    ink_source,
                    word,
                    original_words,
                    source_word_index,
                )
    for index, word in enumerate(visual_words):
        text = _native_word_text(word)
        if not text:
            continue
        is_vertical = "\n" in text
        is_centered = bool(text and text in centered_texts)
        # On the first export every OCR word is unchanged, so a white textbox
        # would erase the original table lines around it. After an editor
        # save, only words that no longer match the original geometry/text
        # need a white patch to hide the old scan glyphs.
        is_edited = not unchanged_words[index]
        # The source image remains authoritative for untouched content. Only
        # changed words get an editable textbox; this keeps thin table rules
        # continuous and avoids a second OCR layer over the scan.
        if original_words is not None and not is_edited:
            continue
        font_size = _editable_textbox_font_size(
            text,
            max(word.width * page_scale, 2.0),
            max(word.height * page_scale, 8.0),
        )
        _append_drawing_textbox(
            paragraph,
            text,
            max(word.left * page_scale, 0.0),
            max(word.top * page_scale, 0.0),
            max(word.width * page_scale, 2.0),
            max(word.height * page_scale, 8.0),
            font_size,
            index,
            # The changed source region was cleared before the textbox was
            # added. Keep the shape transparent so it cannot cover a nearby
            # table rule in Word/LibreOffice.
            transparent=True,
            horizontal_alignment=(
                "center"
                if is_centered or _is_centered_table_word(
                    result,
                    word,
                    table_cell_order_index,
                )
                else "left"
            ),
            bold=is_centered,
            font_color="".join(f"{channel:02X}" for channel in source_ink_colors.get(index, (0, 0, 0))),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    try:
        Document(output_path)
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("生成的原图版式 Word 文档校验失败") from exc
    finally:
        if rendered_background is not None:
            rendered_background.unlink(missing_ok=True)


def _create_image_only_docx(
    result: OcrResult,
    output_path: Path,
    source_image_path: Path,
    *,
    title: str = "扫描识别文档",
) -> None:
    """Create the untouched visual copy without a duplicate OCR layer."""

    with Image.open(source_image_path) as image:
        source_width, source_height = image.size
    page_width_pt, page_height_pt = _image_page_size(source_width, source_height)

    document = Document()
    document.core_properties.title = title or "扫描识别文档"
    section = document.sections[0]
    _set_image_page_size(section, page_width_pt, page_height_pt)
    section.top_margin = Pt(0)
    section.bottom_margin = Pt(0)
    section.left_margin = Pt(0)
    section.right_margin = Pt(0)
    section.header_distance = Pt(0)
    section.footer_distance = Pt(0)
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 0.01
    _append_drawing_image(
        paragraph,
        source_image_path,
        page_width_pt,
        page_height_pt,
        print_resolution_limit=False,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    try:
        Document(output_path)
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("生成的原图版式 Word 文档校验失败") from exc


def _source_checkbox_boxes(source_image_path: Path) -> list[tuple[float, float, float, float]]:
    """Find printed checkbox squares so Word text can use the scan's option anchors."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []

    encoded = np.fromfile(str(source_image_path), dtype=np.uint8)
    gray = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
    if gray is None or gray.size == 0:
        return []
    target_size = max(gray.shape[1] * 0.0123, 6.0)
    minimum_size = max(int(round(target_size * 0.68)), 6)
    maximum_size = max(int(round(target_size * 1.15)), minimum_size + 1)
    binary = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)[1]
    contours, _hierarchy = cv2.findContours(
        binary,
        cv2.RETR_LIST,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    boxes: list[tuple[float, float, float, float]] = []
    for contour in contours:
        left, top, width, height = cv2.boundingRect(contour)
        if not (
            minimum_size <= width <= maximum_size
            and minimum_size <= height <= maximum_size
            and 0.82 <= width / max(height, 1) <= 1.18
        ):
            continue
        area_ratio = cv2.contourArea(contour) / max(width * height, 1)
        if not 0.68 <= area_ratio <= 0.98:
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.08 * perimeter, True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        candidate = (float(left), float(top), float(width), float(height))
        if any(
            abs(candidate[0] - existing[0]) <= 2.0
            and abs(candidate[1] - existing[1]) <= 2.0
            for existing in boxes
        ):
            continue
        boxes.append(candidate)
    return sorted(boxes, key=lambda box: (box[1], box[0]))


def _source_checkbox_states(
    source_image_path: Path,
    boxes: list[tuple[float, float, float, float]],
) -> dict[tuple[int, int], bool]:
    """Read whether each printed square is checked, ignoring its border."""
    if not boxes:
        return {}
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {}
    encoded = np.fromfile(str(source_image_path), dtype=np.uint8)
    gray = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
    if gray is None or gray.size == 0:
        return {}

    states: dict[tuple[int, int], bool] = {}
    for left, top, width, height in boxes:
        x0, y0 = int(round(left)), int(round(top))
        inset = max(int(round(min(width, height) * 0.22)), 2)
        interior = gray[
            y0 + inset : int(round(top + height)) - inset,
            x0 + inset : int(round(left + width)) - inset,
        ]
        if interior.size == 0:
            continue
        dark_pixels = int((interior < 130).sum())
        states[(x0, y0)] = dark_pixels >= max(4, int(round(interior.size * 0.05)))
    return states


def _checkbox_text_option_lines(value: str) -> list[list[str]] | None:
    markers = re.compile(r"([□☑☒✓✔√]+)([^□☑☒✓✔√]*)")
    lines: list[list[str]] = []
    for raw_line in clean_ocr_text(value).splitlines():
        matches = list(markers.finditer(raw_line))
        if not matches:
            continuation = raw_line.strip()
            if continuation and lines and lines[-1]:
                lines[-1][-1] += "\n" + continuation
            elif continuation:
                return None
            continue

        prefix = raw_line[: matches[0].start()].strip()
        if prefix:
            if not lines or not lines[-1]:
                return None
            lines[-1][-1] += "\n" + prefix
        options = [
            f"{match.group(1)}{match.group(2).strip()}"
            for match in matches
        ]
        if any(len(option) == len(match.group(1)) for option, match in zip(options, matches)):
            return None
        lines.append(options)
    return lines or None


def _native_checkbox_states_for_cell(
    image_states: dict[tuple[int, int], bool],
    current_text: str,
    source_cell: TableCell | None,
    original_cells_by_origin: dict[tuple[int, int], TableCell] | None,
) -> dict[tuple[int, int], bool]:
    """Use scan-detected states only while the cell's checkbox states are untouched."""
    if original_cells_by_origin is None:
        return image_states
    if source_cell is None:
        return {}
    original_cell = original_cells_by_origin.get(
        (source_cell.row_start, source_cell.col_start)
    )
    if original_cell is None:
        return {}

    marker_pattern = re.compile(r"[□☑☒✓✔√]")
    checked_markers = {"☑", "☒", "✓", "✔", "√"}
    current_states = tuple(
        marker in checked_markers for marker in marker_pattern.findall(current_text)
    )
    original_states = tuple(
        marker in checked_markers
        for marker in marker_pattern.findall(original_cell.text)
    )
    return image_states if current_states == original_states else {}


def _checkbox_boxes_by_cell_line(
    boxes: list[tuple[float, float, float, float]],
    source_cell: TableCell,
    source_width: int,
) -> list[list[tuple[float, float, float, float]]]:
    if source_cell is None:
        return []
    target_size = max(source_width * 0.0123, 6.0)
    cell_boxes = [
        box
        for box in boxes
        if source_cell.left - 1.5 <= box[0] + box[2] / 2.0 <= source_cell.right + 1.5
        and source_cell.top + 2.0 <= box[1] + box[3] / 2.0 <= source_cell.bottom - 2.0
    ]
    grouped: list[list[tuple[float, float, float, float]]] = []
    for box in sorted(cell_boxes, key=lambda item: (item[1] + item[3] / 2.0, item[0])):
        center_y = box[1] + box[3] / 2.0
        group = next(
            (
                candidate
                for candidate in grouped
                if abs(
                    center_y
                    - sum(item[1] + item[3] / 2.0 for item in candidate) / len(candidate)
                )
                <= target_size * 0.85
            ),
            None,
        )
        if group is None:
            grouped.append([box])
        else:
            group.append(box)
    return [sorted(group, key=lambda item: item[0]) for group in grouped]


def _positioned_checkbox_boxes(
    boxes: list[tuple[float, float, float, float]],
    source_cell: TableCell,
    source_width: int,
    positioned_words: Iterable[PositionedWord] | None,
    expected_count: int,
) -> list[tuple[float, float, float, float]] | None:
    """Recover checkbox anchors that the pixel detector missed in OCR lines."""
    if not positioned_words or expected_count < 3:
        return None
    marker_pattern = re.compile(r"^[□☑☒✓✔√]+")
    marker_words = [
        word
        for word in positioned_words
        if marker_pattern.match(clean_ocr_text(word.text))
    ]
    if len(marker_words) != expected_count:
        return None
    marker_words.sort(key=lambda word: (word.top, word.left))

    target_size = max(source_width * 0.0123, 6.0)
    cell_boxes = [
        box
        for box in boxes
        if source_cell.left - 1.5 <= box[0] + box[2] / 2.0 <= source_cell.right + 1.5
        and source_cell.top + 2.0 <= box[1] + box[3] / 2.0 <= source_cell.bottom - 2.0
    ]
    if len(cell_boxes) < max(2, ceil(expected_count * 0.65)):
        return None

    matched: dict[int, tuple[float, float, float, float]] = {}
    used_box_indices: set[int] = set()
    for word_index, word in enumerate(marker_words):
        word_center_y = word.top + word.height / 2.0
        candidates = []
        for box_index, box in enumerate(cell_boxes):
            if box_index in used_box_indices:
                continue
            vertical_distance = abs(box[1] + box[3] / 2.0 - word_center_y)
            horizontal_offset = box[0] - word.left
            if (
                vertical_distance <= target_size * 0.8
                and -target_size * 0.25 <= horizontal_offset <= target_size * 1.5
            ):
                candidates.append((vertical_distance, abs(horizontal_offset), box_index))
        if candidates:
            _vertical_distance, _horizontal_distance, box_index = min(candidates)
            matched[word_index] = cell_boxes[box_index]
            used_box_indices.add(box_index)

    if len(matched) < max(2, ceil(expected_count * 0.65)):
        return None

    horizontal_offsets = [
        box[0] - marker_words[index].left
        for index, box in matched.items()
    ]
    vertical_offsets = [
        box[1] - marker_words[index].top
        for index, box in matched.items()
    ]
    offset_x = median(horizontal_offsets)
    offset_y = median(vertical_offsets)
    if any(
        abs(offset - offset_x) > target_size * 0.5
        for offset in horizontal_offsets
    ) or any(
        abs(offset - offset_y) > target_size * 0.5
        for offset in vertical_offsets
    ):
        return None

    box_width = median([box[2] for box in matched.values()])
    box_height = median([box[3] for box in matched.values()])
    completed: list[tuple[float, float, float, float]] = []
    for index, word in enumerate(marker_words):
        box = matched.get(index)
        if box is None:
            box = (
                word.left + offset_x,
                word.top + offset_y,
                box_width,
                box_height,
            )
        if not (
            source_cell.left - 1.5 <= box[0] + box[2] / 2.0 <= source_cell.right + 1.5
            and source_cell.top + 2.0 <= box[1] + box[3] / 2.0 <= source_cell.bottom - 2.0
        ):
            return None
        completed.append(box)
    return completed


def _layout_native_checkbox_text(
    value: str,
    source_cell: TableCell | None,
    boxes: list[tuple[float, float, float, float]],
    source_width: int,
    page_scale: float,
    checkbox_states: dict[tuple[int, int], bool] | None = None,
    positioned_words: Iterable[PositionedWord] | None = None,
) -> _NativeCheckboxLayout | None:
    """Use source checkbox x-coordinates as native Word tab stops."""
    if source_cell is None or not boxes:
        return None
    positioned_word_list = list(positioned_words or ())

    # The family-income entry sits below the last checklist item in the same
    # merged cell. Keep it as its own paragraph instead of accidentally
    # appending it to the final checkbox label.
    footer_start = value.rfind("家庭人均年收入")
    footer_text = ""
    option_text = value
    if footer_start > 0 and "□" in value[:footer_start]:
        option_text = value[:footer_start].rstrip()
        footer_text = value[footer_start:].strip()

    option_lines = _checkbox_text_option_lines(option_text)
    if not option_lines:
        return None
    expected_count = sum(len(line) for line in option_lines)
    box_lines = _checkbox_boxes_by_cell_line(boxes, source_cell, source_width)
    detected_count = sum(len(line) for line in box_lines)
    if detected_count != expected_count:
        positioned_boxes = _positioned_checkbox_boxes(
            boxes,
            source_cell,
            source_width,
            positioned_word_list,
            expected_count,
        )
        if positioned_boxes is not None:
            box_lines = _checkbox_boxes_by_cell_line(
                positioned_boxes,
                source_cell,
                source_width,
            )
            detected_count = sum(len(line) for line in box_lines)
    target_size = max(source_width * 0.0123, 6.0)

    # A text glyph can resemble a small hollow square. When a one-option row
    # has extra candidates, the printed checkbox is the square nearest the
    # cell's normal first-option inset.
    if detected_count > expected_count == 1 and len(box_lines) == 1:
        expected_left = source_cell.left + target_size * 0.95
        box_lines = [[min(box_lines[0], key=lambda box: abs(box[0] - expected_left))]]
        detected_count = 1
    if detected_count != expected_count:
        return None

    if len(box_lines) != len(option_lines):
        if len(option_lines) == 1 and len(box_lines) > 1:
            flat_options = option_lines[0]
            if [len(line) for line in box_lines] and sum(len(line) for line in box_lines) == len(flat_options):
                option_lines = []
                offset = 0
                for box_line in box_lines:
                    option_lines.append(flat_options[offset : offset + len(box_line)])
                    offset += len(box_line)
        elif sum(len(line) for line in box_lines) == expected_count:
            flat_options = [option for line in option_lines for option in line]
            option_lines = []
            offset = 0
            for box_line in box_lines:
                option_lines.append(flat_options[offset : offset + len(box_line)])
                offset += len(box_line)
        if len(box_lines) != len(option_lines):
            return None

    normalized_option_lines: list[list[str]] = []
    for option_line, box_line in zip(option_lines, box_lines):
        if len(option_line) != len(box_line):
            return None
        normalized_line: list[str] = []
        for option, box in zip(option_line, box_line):
            checked = (
                (checkbox_states or {}).get((int(round(box[0])), int(round(box[1]))))
            )
            if checked is not None:
                label = re.sub(r"^[□☑☒✓✔√]+", "", option)
                option = ("☑" if checked else "□") + label
            normalized_line.append(option)
        normalized_option_lines.append(normalized_line)
    first_lefts = [line[0][0] for line in box_lines if line]
    if not first_lefts or max(first_lefts) - min(first_lefts) > max(target_size * 0.25, 2.0):
        return None

    first_left = first_lefts[0]
    cell_margin_pt = 45 / 20.0
    paragraph_indent_pt = max((first_left - source_cell.left) * page_scale - cell_margin_pt, 0.0)
    tab_stops = sorted(
        {
            round((box[0] - first_left) * page_scale + paragraph_indent_pt, 2)
            for line in box_lines
            for box in line[1:]
            if box[0] > first_left
        }
    )
    formatted_lines = ["\t".join(line) for line in normalized_option_lines]
    footer_tab_position_pt: float | None = None
    footer_space_after_pt = 0.0
    if footer_text:
        # Keep the income field on its own row and match the scan's wrap after
        # “人民币”, instead of letting it join the final checkbox item. The
        # empty entry below is a spacing marker; its height comes from OCR y
        # coordinates and is applied to the preceding Word paragraph.
        formatted_lines.append("")
        footer_text = re.sub(r"\s+", " ", footer_text).strip()
        footer_label_word = next(
            (
                word
                for word in positioned_word_list
                if "家庭人均年收入" in "".join(clean_ocr_text(word.text).split())
            ),
            None,
        )
        footer_unit_word = next(
            (
                word
                for word in positioned_word_list
                if "人民币" in "".join(clean_ocr_text(word.text).split())
            ),
            None,
        )
        last_option_text = re.sub(
            r"^[□☑☒✓✔√]+", "", normalized_option_lines[-1][-1]
        )
        last_option_key = "".join(clean_ocr_text(last_option_text).split())
        last_option_word = max(
            (
                word
                for word in positioned_word_list
                if last_option_key
                in "".join(
                    re.sub(
                        r"^[□☑☒✓✔√]+",
                        "",
                        clean_ocr_text(word.text),
                    ).split()
                )
                and (
                    footer_label_word is None
                    or word.top < footer_label_word.top
                )
            ),
            key=lambda word: word.top,
            default=None,
        )
        if footer_label_word is not None and footer_unit_word is not None:
            unit_left = footer_unit_word.left
            footer_left = footer_label_word.left
            same_line_tolerance = max(target_size, footer_label_word.height)
            if (
                abs(footer_left - first_left) <= max(target_size, 8.0)
                and abs(footer_unit_word.top - footer_label_word.top)
                <= same_line_tolerance
                and unit_left > footer_left + footer_label_word.width
                and unit_left + footer_unit_word.width < source_cell.right
            ):
                footer_tab_position_pt = round(
                    (unit_left - first_left) * page_scale + paragraph_indent_pt,
                    2,
                )
                if not 0 < footer_tab_position_pt < (
                    (source_cell.right - first_left) * page_scale
                    + paragraph_indent_pt
                ):
                    footer_tab_position_pt = None

        if footer_tab_position_pt is not None and footer_unit_word is not None:
            unit_text = clean_ocr_text(footer_unit_word.text)
            formatted_lines.append("家庭人均年收入\t" + unit_text)
            if "元）" in footer_text and "元）" not in unit_text:
                formatted_lines.append("元）")
        else:
            footer_text = footer_text.replace(
                "家庭人均年收入", "家庭人均年收入" + "_" * 15, 1
            )
            if "（人民币" in footer_text and "元）" in footer_text:
                formatted_lines.extend(
                    [footer_text.split("元）", 1)[0].rstrip(), "元）"]
                )
            else:
                formatted_lines.append(footer_text)
    formatted_text = "\n".join(formatted_lines)
    source_line_height = target_size * 2.0 * page_scale
    # Match the printed type size from OCR word heights, then constrain it to
    # the measured width of each option so text does not spill into adjacent
    # columns or wrap an extra line inside a fixed-height source row.
    option_label_keys = [
        "".join(
            clean_ocr_text(re.sub(r"^[□☑☒✓✔√]+", "", option)).split()
        )
        for line in normalized_option_lines
        for option in line
    ]
    option_word_heights = []
    option_width_limits = []
    for word in positioned_word_list:
        word_height = float(word.height)
        if not isfinite(word_height) or word_height <= 0:
            continue
        word_key = "".join(
            re.sub(
                r"^[□☑☒✓✔√]+",
                "",
                clean_ocr_text(word.text),
            ).split()
        )
        matching_option_keys = [
            option_key
            for option_key in option_label_keys
            if option_key and option_key in word_key
        ]
        if matching_option_keys:
            option_word_heights.append(word_height)
        if word_key in option_label_keys:
            word_lines = clean_ocr_text(word.text).splitlines()
            line_width_units = [
                sum(
                    0.5 if character.isascii() else 1.0
                    for character in re.sub(
                        r"^[□☑☒✓✔√]+", "", line
                    )
                    if not character.isspace()
                )
                for line in word_lines
            ]
            widest_line_units = max(line_width_units, default=0.0)
            if widest_line_units > 0:
                marker_width = (
                    target_size
                    if re.match(r"^[□☑☒✓✔√]+", clean_ocr_text(word.text))
                    else 0.0
                )
                available_width_pt = max(
                    word.width - marker_width,
                    0.0,
                ) * page_scale
                if available_width_pt > 0:
                    option_width_limits.append(
                        available_width_pt / widest_line_units
                    )
    font_size_pt = min(
        12.0,
        max(
            6.0,
            median(option_word_heights) * page_scale
            if option_word_heights
            else 9.5,
        ),
    )
    if option_width_limits:
        font_size_pt = max(6.0, min(font_size_pt, min(option_width_limits)))
    if source_line_height > 0:
        font_size_pt = min(font_size_pt, max(6.0, source_line_height * 0.9))
    source_checkbox_top = (
        median(box[1] for box in box_lines[0])
        if box_lines
        else source_cell.top
    )
    # Word's line box has a small built-in ascent inset. Subtract it from the
    # measured source gap so the checkbox glyph lands on the scan's y-coordinate.
    top_padding_pt = max(
        (source_checkbox_top - source_cell.top) * page_scale - font_size_pt * 0.08,
        0.0,
    )
    if footer_text and last_option_word is not None and footer_label_word is not None:
        # A Word line already advances by source_line_height. Add only the
        # measured remainder so the income label starts at its scanned y.
        source_gap_pt = max(
            footer_label_word.top - last_option_word.top,
            0.0,
        ) * page_scale
        footer_space_after_pt = max(source_gap_pt - source_line_height, 0.0)
    elif footer_text:
        footer_space_after_pt = source_line_height * 0.85
    return _NativeCheckboxLayout(
        text=formatted_text,
        left_indent_pt=paragraph_indent_pt,
        top_padding_pt=top_padding_pt,
        checkbox_tab_stops=tab_stops,
        paragraph_texts=formatted_lines,
        line_spacing_pt=source_line_height,
        font_size_pt=font_size_pt,
        footer_tab_position_pt=footer_tab_position_pt,
        footer_space_after_pt=footer_space_after_pt,
    )


def _apply_native_checkbox_layout(
    cell,
    layout: _NativeCheckboxLayout,
) -> None:
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    for existing_paragraph in list(cell.paragraphs):
        existing_paragraph._element.getparent().remove(existing_paragraph._element)

    previous_paragraph = None
    for paragraph_text in layout.paragraph_texts:
        if paragraph_text == "":
            if previous_paragraph is not None:
                previous_paragraph.paragraph_format.space_after = Pt(
                    layout.footer_space_after_pt
                )
            continue
        paragraph = cell.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        paragraph.paragraph_format.left_indent = Pt(layout.left_indent_pt)
        paragraph.paragraph_format.space_before = Pt(
            layout.top_padding_pt if previous_paragraph is None else 0
        )
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = Pt(layout.line_spacing_pt)
        for position in layout.checkbox_tab_stops:
            paragraph.paragraph_format.tab_stops.add_tab_stop(Pt(position))
        if (
            layout.footer_tab_position_pt is not None
            and paragraph_text.startswith("家庭人均年收入\t")
        ):
            # The leader draws the printed fill-in rule while the tab keeps
            # the unit label anchored at its measured x-coordinate.
            paragraph.paragraph_format.tab_stops.add_tab_stop(
                Pt(layout.footer_tab_position_pt),
                WD_TAB_ALIGNMENT.LEFT,
                WD_TAB_LEADER.LINES,
            )
        run = paragraph.add_run(paragraph_text)
        run.font.name = "SimSun"
        run.font.size = Pt(layout.font_size_pt)
        run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "SimSun")
        run_properties = run._element.get_or_add_rPr()
        vertical_offset = OxmlElement("w:position")
        # Word's square glyph sits slightly above the scanned checkbox unless
        # the line is raised. The v18 render measured about 9px too high at
        # the page's 1605px width; 3.5pt (7 half-points) corrects that offset.
        vertical_offset.set(qn("w:val"), "7")
        run_properties.append(vertical_offset)
        previous_paragraph = paragraph


def create_hybrid_editable_docx(
    result: OcrResult,
    output_path: Path,
    source_image_path: Path | None,
    *,
    title: str = "扫描识别文档",
) -> None:
    """Create a high-fidelity Word table with editable native cell text.

    The scan remains the page background so blank fields, merged-cell geometry
    and thin rules keep their source coordinates. Text inside table cells is
    cleared from a temporary background and rebuilt as real Word cell text.
    Text outside the tables (especially the title and form header) is cleared
    and rebuilt as editable DrawingML text boxes as well; otherwise the
    structured Word would visibly contain the title but editing it would leave
    the old title baked into the background image.
    """

    if not result.tables or not source_image_path or not source_image_path.exists():
        create_docx(result, output_path, None, title=title)
        return

    with Image.open(source_image_path) as image:
        source_width, source_height = image.size
    page_width_pt, page_height_pt = _image_page_size(source_width, source_height)
    page_scale = page_width_pt / max(source_width, 1)
    checkbox_boxes = _source_checkbox_boxes(source_image_path)
    checkbox_states = _source_checkbox_states(source_image_path, checkbox_boxes)

    with tempfile.NamedTemporaryFile(
        prefix="docx_editable_table_",
        suffix=".png",
        dir=source_image_path.parent,
        delete=False,
    ) as temporary:
        background_path = Path(temporary.name)
    try:
        cleaned_text_by_word_id: dict[int, str] = {}
        positioned_words_by_cell = _positioned_words_by_table_cell(
            result.table_cells,
            result.positioned_words,
            cleaned_text_by_word_id,
        )
        _create_hybrid_editable_background(
            result,
            source_image_path,
            background_path,
            clear_all_table_ink=True,
        )

        document = Document()
        document.core_properties.title = title or "扫描识别文档"
        section = document.sections[0]
        _set_image_page_size(section, page_width_pt, page_height_pt)
        section.top_margin = Pt(0)
        section.bottom_margin = Pt(0)
        section.left_margin = Pt(0)
        section.right_margin = Pt(0)
        section.header_distance = Pt(0)
        section.footer_distance = Pt(0)

        background_paragraph = document.add_paragraph()
        background_paragraph.paragraph_format.space_before = Pt(0)
        background_paragraph.paragraph_format.line_spacing = 0.01
        _append_drawing_image(
            background_paragraph,
            background_path,
            page_width_pt,
            page_height_pt,
        )

        # Keep every OCR word outside a table editable too. Table cell text is
        # represented by the native table below; drawing it a second time
        # would create duplicate glyphs and make the cell look darker.
        table_bounds = _table_bounds(result)
        centered_texts = _centered_paragraph_texts(result)
        outside_words = [
            word
            for word in result.positioned_words
            if _native_word_text(word)
            and not _word_overlaps_table(word, table_bounds)
        ]
        for index, word in enumerate(outside_words):
            text = _native_word_text(word)
            width_pt = max(word.width * page_scale, 2.0)
            height_pt = max(word.height * page_scale, 8.0)
            _append_drawing_textbox(
                background_paragraph,
                text,
                max(word.left * page_scale, 0.0),
                max(word.top * page_scale, 0.0),
                width_pt,
                height_pt,
                _editable_textbox_font_size(text, width_pt, height_pt),
                index,
                transparent=True,
                horizontal_alignment="center" if text in centered_texts else "left",
                bold=False,
            )

        previous_bottom = 0.0
        for table_index, table_data in enumerate(result.tables):
            if not table_data:
                continue
            layout = (
                result.table_layouts[table_index]
                if table_index < len(result.table_layouts)
                else TableLayout()
            )
            cells = (
                result.table_cells[table_index]
                if table_index < len(result.table_cells)
                else []
            )
            cells_by_origin = _table_cells_by_origin(cells)
            valid_cells = [
                cell
                for cell in cells
                if cell.right > cell.left and cell.bottom > cell.top
            ]
            if not valid_cells:
                continue
            table_top = min(cell.top for cell in valid_cells)
            table_bottom = max(cell.bottom for cell in valid_cells)
            spacer = document.add_paragraph()
            spacer.paragraph_format.space_before = Pt(0)
            spacer.paragraph_format.space_after = Pt(
                # LibreOffice/Word reserves a small leading block before a
                # flowing table even when the preceding paragraph is nearly
                # zero-height. Compensate for that fixed flow gap so the
                # native editable cells land on the source table rules.
                max((table_top - previous_bottom) * page_scale - 25.0, 0.0)
            )
            spacer.paragraph_format.line_spacing = 0.01

            row_count = len(table_data)
            column_count = max(len(row) for row in table_data)
            table = document.add_table(rows=row_count, cols=column_count)
            table.autofit = False
            _remove_table_borders(table)
            table_width_emu = int(
                max((layout.source_right - layout.source_left) * page_scale, 1.0)
                * EMU_PER_PT
            )
            table_indent_emu = int(max(layout.source_left * page_scale, 0.0) * EMU_PER_PT)
            column_widths = _column_widths(layout, column_count, table_width_emu)
            for column_index, width in enumerate(column_widths):
                table.columns[column_index].width = width
                for cell in table.columns[column_index].cells:
                    cell.width = width
            _set_table_width(table, table_width_emu, table_indent_emu)
            _set_cell_margins(table, top=0, start=45, bottom=0, end=45)

            for row_index, row in enumerate(table_data):
                if row_index < len(layout.row_heights):
                    row_height = int(layout.row_heights[row_index] * page_scale * EMU_PER_PT)
                    table.rows[row_index].height = max(row_height, int(Cm(0.25)))
                    table.rows[row_index].height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
                for column_index in range(column_count):
                    value = row[column_index] if column_index < len(row) else ""
                    source_cell = cells_by_origin.get((row_index, column_index))
                    cell = table.cell(row_index, column_index)
                    cell_text = _native_cell_text(
                        result,
                        source_cell,
                        value,
                        positioned_words_by_cell.get(id(source_cell), ()),
                        cleaned_text_by_word_id,
                    )
                    checkbox_layout = _layout_native_checkbox_text(
                        cell_text,
                        source_cell,
                        checkbox_boxes,
                        source_width,
                        page_scale,
                        checkbox_states,
                        positioned_words=positioned_words_by_cell.get(id(source_cell), ()),
                    )
                    cell_words = positioned_words_by_cell.get(id(source_cell), ())
                    cell.text = checkbox_layout.text if checkbox_layout else cell_text
                    cell.vertical_alignment = _native_cell_vertical_alignment(
                        source_cell,
                        cell_words,
                    )
                    _set_vertical_section_label(
                        cell,
                        source_cell,
                        cell.text,
                        cell_words,
                    )
                    _style_cell(
                        cell,
                        False,
                        horizontal_alignment=(
                            WD_ALIGN_PARAGRAPH.LEFT
                            if checkbox_layout
                            else _native_cell_horizontal_alignment(
                                source_cell,
                                cell_words,
                                cell_text,
                            )
                        ),
                    )
                    if checkbox_layout:
                        _apply_native_checkbox_layout(cell, checkbox_layout)

            for source_cell in _native_merge_plan(cells, row_count, column_count):
                merge_span = _native_merge_span(
                    source_cell,
                    row_count,
                    column_count,
                )
                if merge_span is None:
                    continue
                row_start, row_end, col_start, col_end = merge_span
                merged = table.cell(
                    row_start,
                    col_start,
                ).merge(
                    table.cell(row_end - 1, col_end - 1),
                )
                source_cell_text = _native_cell_text(
                    result,
                    source_cell,
                    source_cell.text,
                    positioned_words_by_cell.get(id(source_cell), ()),
                    cleaned_text_by_word_id,
                )
                cell_text = _preserve_native_merged_text(merged.text, source_cell_text)
                checkbox_layout = _layout_native_checkbox_text(
                    cell_text,
                    source_cell,
                    checkbox_boxes,
                    source_width,
                    page_scale,
                    checkbox_states,
                    positioned_words=positioned_words_by_cell.get(id(source_cell), ()),
                )
                source_cell_words = positioned_words_by_cell.get(id(source_cell), ())
                merged.text = checkbox_layout.text if checkbox_layout else cell_text
                merged.vertical_alignment = _native_cell_vertical_alignment(
                    source_cell,
                    source_cell_words,
                )
                _set_vertical_section_label(
                    merged,
                    source_cell,
                    merged.text,
                    source_cell_words,
                )
                _style_cell(
                    merged,
                    False,
                    horizontal_alignment=(
                        WD_ALIGN_PARAGRAPH.LEFT
                        if checkbox_layout
                        else _native_cell_horizontal_alignment(
                            source_cell,
                            source_cell_words,
                            cell_text,
                        )
                    ),
                )
                if checkbox_layout:
                    _apply_native_checkbox_layout(merged, checkbox_layout)
            previous_bottom = table_bottom

        output_path.parent.mkdir(parents=True, exist_ok=True)
        document.save(output_path)
        try:
            Document(output_path)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("生成的可编辑表格 Word 文档校验失败") from exc
    finally:
        background_path.unlink(missing_ok=True)


def _create_hybrid_editable_background(
    result: OcrResult,
    source_image_path: Path,
    output_path: Path,
    *,
    clear_all_table_ink: bool = False,
) -> None:
    """Clear table cells and outside-table OCR text for editable overlays."""

    with Image.open(source_image_path) as source:
        original = _image_on_white(source)
        canvas = original.copy()
    for cells in result.table_cells:
        for cell in cells:
            _clear_hybrid_table_cell_text(
                canvas,
                original,
                cell,
                result.positioned_words,
                clear_all_ink=clear_all_table_ink,
            )
    _redraw_table_cell_lines(canvas, result)

    table_bounds = _table_bounds(result)
    centered_texts = _centered_paragraph_texts(result)
    try:
        line_guides = (
            detect_table_line_guides(
                source_image_path,
                (0.0, 0.0, float(canvas.width), float(canvas.height)),
            )
            if table_bounds
            else LineGuides([], [])
        )
    except Exception:
        line_guides = LineGuides([], [])
    for word in result.positioned_words:
        if not _native_word_text(word) or _word_overlaps_table(word, table_bounds):
            continue
        text_left = max(int(round(word.left)), 0)
        text_top = max(int(round(word.top)), 0)
        text_right = min(int(round(word.left + word.width)), canvas.width)
        text_bottom = min(int(round(word.top + word.height)), canvas.height)
        horizontal_padding = max(1, min(3, int(round(word.height * 0.08))))
        top_padding = max(2, min(5, int(round(word.height * 0.12))))
        bottom_padding = max(1, min(2, int(round(word.height * 0.05))))
        left = max(text_left - horizontal_padding, 0)
        top = max(text_top - top_padding, 0)
        right = min(text_right + horizontal_padding, canvas.width)
        bottom = min(text_bottom + bottom_padding, canvas.height)
        if right <= left or bottom <= top:
            continue
        _fill_paper_region(canvas, original, left, top, right, bottom)
        # Title and subtitle glyphs are commonly detected as short vertical
        # guides by the line detector. Restoring those guides after clearing
        # the OCR box leaves black fragments behind the editable title. The
        # title textbox is rebuilt above the background, so keep this region
        # clean; ordinary form labels still restore nearby underline rules.
        if clean_ocr_text(word.text) not in centered_texts:
            _restore_table_lines(
                canvas,
                original,
                line_guides,
                left,
                top,
                right,
                bottom,
                table_cells=result.table_cells,
            )

    # Redraw semantic cell rules after outside-text cleanup as well. A header
    # line can be close enough to a title box that the generic line detector
    # restores only part of it.
    _redraw_table_cell_lines(canvas, result)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)


def _remove_table_borders(table) -> None:
    """Make the native table transparent so the cleaned scan supplies rules."""

    table_properties = table._tbl.tblPr
    borders = table_properties.first_child_found_in("w:tblBorders")
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        table_properties.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = borders.find(qn(f"w:{edge}"))
        if border is None:
            border = OxmlElement(f"w:{edge}")
            borders.append(border)
        border.set(qn("w:val"), "nil")
    for row in table.rows:
        for cell in row.cells:
            cell_borders = cell._tc.get_or_add_tcPr().first_child_found_in("w:tcBorders")
            if cell_borders is None:
                cell_borders = OxmlElement("w:tcBorders")
                cell._tc.get_or_add_tcPr().append(cell_borders)
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
                border = cell_borders.find(qn(f"w:{edge}"))
                if border is None:
                    border = OxmlElement(f"w:{edge}")
                    cell_borders.append(border)
                border.set(qn("w:val"), "nil")


def _empty_table_cell_words(
    result: OcrResult,
    words: list[PositionedWord],
) -> list[PositionedWord]:
    """Create transparent Word editing targets for blank source cells."""

    occupied = {
        (table_index, row_start, col_start)
        for table_index, row_start, col_start, _cell_id in _table_cell_memberships(
            result.table_cells,
            words,
        )
    }

    overlays: list[PositionedWord] = []
    for table_index, cells in enumerate(result.table_cells):
        for cell in cells:
            if cell.right <= cell.left or cell.bottom <= cell.top:
                continue
            if clean_ocr_text(cell.text) or (
                table_index,
                cell.row_start,
                cell.col_start,
            ) in occupied:
                continue
            overlays.append(
                PositionedWord(
                    text="",
                    left=cell.left + 1.0,
                    top=cell.top + 1.0,
                    width=max(cell.right - cell.left - 2.0, 8.0),
                    height=max(cell.bottom - cell.top - 2.0, 12.0),
                )
            )
    return overlays


def _visual_edit_regions(
    result: OcrResult,
    visual_words: list[PositionedWord],
    original_words: list[PositionedWord] | None,
) -> tuple[list[PositionedWord], list[TableCell]]:
    """Return only source regions that need replacement after an edit."""

    if original_words is None:
        return list(visual_words), []

    original_index = _coordinate_word_index(original_words)
    visual_index = _coordinate_word_index(visual_words)
    changed_words = [
        word
        for word in visual_words
        if clean_ocr_text(word.text)
        and not _has_same_positioned_word(word, original_words, original_index)
    ]
    changed_memberships = _table_cell_memberships(
        result.table_cells,
        changed_words,
        require_text=True,
    )
    changed_cell_ids = {cell_id for *_key, cell_id in changed_memberships}
    original_words_by_cell = _positioned_words_by_table_cell(
        result.table_cells,
        original_words,
    )
    partially_edited_cell_ids = {
        cell_id
        for cell_id, cell_words in original_words_by_cell.items()
        if any(
            _has_same_positioned_word(word, visual_words, visual_index)
            for word in cell_words
        )
    }
    # A table-cell edit replaces every OCR fragment in the cell. A coordinate
    # edit may replace just one fragment; in that case clearing the whole cell
    # would also remove neighboring words that are intentionally unchanged.
    changed_cell_ids.difference_update(partially_edited_cell_ids)
    changed_cells: list[TableCell] = []
    for cells in result.table_cells:
        for cell in cells:
            if cell.right <= cell.left or cell.bottom <= cell.top:
                continue
            if id(cell) in changed_cell_ids:
                changed_cells.append(cell)

    erase_words = list(changed_words)
    changed_cell_index = _table_cell_coordinate_index([changed_cells])
    for original in original_words:
        has_unchanged_replacement = _has_same_positioned_word(
            original,
            visual_words,
            visual_index,
        )
        inside_changed_cell = _table_contains_center(
            original.left + original.width / 2.0,
            original.top + original.height / 2.0,
            changed_cell_index,
        )
        if not has_unchanged_replacement or inside_changed_cell:
            erase_words.append(original)
    return erase_words, changed_cells


def _missing_table_cell_words(
    result: OcrResult,
    words: list[PositionedWord],
    source_table_cells: list[list[TableCell]] | None = None,
    source_positioned_words: list[PositionedWord] | None = None,
) -> list[PositionedWord]:
    """Recover cell text that exists only in the table OCR response."""

    if source_table_cells is None:
        return []

    positioned_text_cells = _table_cell_memberships(
        result.table_cells,
        words,
        require_text=True,
    )
    overlays: list[PositionedWord] = []
    for table_index, cells in enumerate(result.table_cells):
        if table_index >= len(source_table_cells):
            continue
        source_cells_by_origin = {
            (cell.row_start, cell.col_start, cell.row_end, cell.col_end): cell
            for cell in source_table_cells[table_index]
        }
        for cell in cells:
            if cell.right <= cell.left or cell.bottom <= cell.top:
                continue
            text = clean_ocr_text(cell.text)
            if not text:
                continue
            has_positioned_text = (
                table_index,
                cell.row_start,
                cell.col_start,
                id(cell),
            ) in positioned_text_cells
            if has_positioned_text:
                continue
            source_cell = source_cells_by_origin.get(
                (cell.row_start, cell.col_start, cell.row_end, cell.col_end)
            )
            if source_cell is None:
                continue
            source_words = [
                word
                for word in (source_positioned_words or [])
                if (
                    source_cell.left
                    <= word.left + word.width / 2.0
                    <= source_cell.right
                    and source_cell.top
                    <= word.top + word.height / 2.0
                    <= source_cell.bottom
                )
            ]
            expected_source_text = normalize_table_cell_value(
                source_cell.text,
                source_words,
            )
            compact_current = "".join(clean_ocr_text(cell.text).split())
            compact_source = "".join(clean_ocr_text(expected_source_text).split())
            if compact_current == compact_source:
                continue
            overlays.append(
                PositionedWord(
                    text=text,
                    left=cell.left + 1.0,
                    top=cell.top + 1.0,
                    width=max(cell.right - cell.left - 2.0, 8.0),
                    height=max(cell.bottom - cell.top - 2.0, 12.0),
                )
            )
    return overlays


def _centered_paragraph_texts(result: OcrResult) -> set[str]:
    return {
        clean_ocr_text(text)
        for text, style, _ in _paragraph_blocks(result)
        if style in {"title", "subtitle"} and clean_ocr_text(text)
    }


def _is_centered_table_word(
    result: OcrResult,
    word: PositionedWord,
    table_cell_order_index: tuple[
        tuple[dict[tuple[int, int], list[TableCell]], list[TableCell]],
        dict[int, int],
    ] | None = None,
    original_words: list[PositionedWord] | None = None,
    source_word_index: tuple[dict[tuple[int, int], list[int]], float] | None = None,
) -> bool:
    """Infer cell alignment from the original OCR box after an edit."""

    alignment_word = word
    if original_words:
        source_index = _match_source_word_index(
            word,
            original_words,
            source_word_index,
        )
        if source_index is not None:
            alignment_word = original_words[source_index]

    word_center_x = float(alignment_word.left) + float(alignment_word.width) / 2.0
    word_center_y = float(alignment_word.top) + float(alignment_word.height) / 2.0
    index = table_cell_order_index or _table_cell_order_index(result.table_cells)
    for cell in _table_cells_ordered_at_center(word_center_x, word_center_y, index):
        cell_width = float(cell.right) - float(cell.left)
        cell_height = float(cell.bottom) - float(cell.top)
        if cell_width <= 0 or cell_height <= 0:
            continue
        if not (
            cell.left <= word_center_x <= cell.right
            and cell.top <= word_center_y <= cell.bottom
        ):
            continue
        # Long option rows are intentionally left aligned. Short labels,
        # IDs and names usually have nearly equal side margins in the
        # source form; keep that geometry for edited replacements too.
        if float(alignment_word.width) > cell_width * 1.05:
            return False
        left_margin = float(alignment_word.left) - float(cell.left)
        right_margin = float(cell.right) - (
            float(alignment_word.left) + float(alignment_word.width)
        )
        return abs(left_margin - right_margin) <= max(8.0, cell_width * 0.15)
    return False


def render_positioned_image(
    result: OcrResult,
    source_image_path: Path,
    output_path: Path,
    original_words: list | None = None,
) -> None:
    """Render edited OCR boxes onto the scan for a faithful PDF export."""

    with Image.open(source_image_path) as source:
        original = _image_on_white(source)
        canvas = original.copy()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    precleared = False
    if original_words is not None:
        erase_words, changed_cells = _visual_edit_regions(
            result,
            result.positioned_words,
            original_words,
        )
        if erase_words:
            with tempfile.TemporaryDirectory(
                prefix="positioned_image_background_",
                dir=output_path.parent,
            ) as temp_dir:
                background_path = Path(temp_dir) / "background.png"
                _erase_positioned_text(
                    result,
                    source_image_path,
                    background_path,
                    words_to_erase=erase_words,
                    clear_cells=changed_cells,
                )
                with Image.open(background_path) as background:
                    cleaned_canvas = _image_on_white(background)
                canvas.close()
                canvas = cleaned_canvas
            precleared = True
    draw = ImageDraw.Draw(canvas)
    font_path = _render_font_path()
    table_bounds = _table_bounds(result)
    try:
        line_guides = (
            detect_table_line_guides(
                source_image_path,
                (0.0, 0.0, float(canvas.width), float(canvas.height)),
            )
            if table_bounds
            else LineGuides([], [])
        )
    except Exception:
        line_guides = LineGuides([], [])
    cell_guides = _table_cell_line_guides(result)
    if cell_guides.x or cell_guides.y:
        # OCR table cells provide the authoritative grid geometry. The generic
        # pixel detector can mistake repeated Chinese strokes for a vertical
        # rule and paste those strokes back into an otherwise clean cell.
        line_guides = cell_guides
    centered_texts = _centered_paragraph_texts(result)
    table_cell_order_index = _table_cell_order_index(result.table_cells)
    original_coordinate_index = _coordinate_word_index(original_words or [])
    source_word_index = (
        _build_source_word_spatial_index(original_words)
        if original_words
        else None
    )
    for index, word in enumerate(result.positioned_words):
        # The coordinate editor sends non-table overlays first and then the
        # untouched table words. Their list order is therefore not guaranteed
        # to match the original OCR order. Compare geometry as well as text;
        # using the list index here redraws every untouched table word and
        # leaves white holes/dots in the exported PDF.
        if original_words and _has_same_positioned_word(
            word,
            original_words,
            original_coordinate_index,
        ):
            continue
        text_left = max(int(round(word.left)), 0)
        text_top = max(int(round(word.top)), 0)
        text_right = min(int(round(word.left + word.width)), canvas.width)
        text_bottom = min(int(round(word.top + word.height)), canvas.height)
        horizontal_padding = max(1, min(3, int(round(word.height * 0.08))))
        top_padding = max(2, min(5, int(round(word.height * 0.12))))
        bottom_padding = max(1, min(2, int(round(word.height * 0.05))))
        left = max(text_left - horizontal_padding, 0)
        top = max(text_top - top_padding, 0)
        right = min(text_right + horizontal_padding, canvas.width)
        bottom = min(text_bottom + bottom_padding, canvas.height)
        if right <= left or bottom <= top:
            continue
        if not precleared:
            _inpaint_positioned_region(canvas, original, left, top, right, bottom)
        _restore_table_lines(
            canvas,
            original,
            line_guides,
            left,
            top,
            right,
            bottom,
            table_bounds,
            result.table_cells,
        )
        text = clean_ocr_text(word.text)
        if not text:
            continue
        font, lines = _fit_render_text(draw, text, right - left - 2, bottom - top - 2, font_path)
        font_color = _source_ink_rgb(
            original,
            word,
            original_words,
            source_word_index,
        )
        rendered_text = "\n".join(lines)
        centered_table_word = _is_centered_table_word(
            result,
            word,
            table_cell_order_index,
            original_words,
            source_word_index,
        )
        text_layer = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
        text_layer_draw = ImageDraw.Draw(text_layer)
        if text in centered_texts or centered_table_word:
            # Draw into a region-sized layer so wrapped text cannot overwrite
            # the next source line. The box center matches the coordinate
            # editor's preserved source center. Pillow 11 no longer accepts
            # a top/bottom anchor, so center the block with its middle anchor.
            text_layer_draw.multiline_text(
                (text_layer.width / 2.0, text_layer.height / 2.0),
                rendered_text,
                font=font,
                fill=font_color,
                spacing=0,
                anchor="mm",
                align="center",
            )
        else:
            text_layer_draw.multiline_text(
                (1, 1),
                rendered_text,
                font=font,
                fill=font_color,
                spacing=0,
            )
        canvas.paste(text_layer, (left, top), text_layer.getchannel("A"))
        text_layer.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # The cleaned background is mostly white with one-pixel rules. A JPEG
    # round-trip creates grey blocks around those rules and faint remnants
    # become visible after Word scales the page. Keep this intermediate layer
    # lossless; the original source image can remain JPEG elsewhere.
    canvas.save(output_path, format="PNG", optimize=True)


def _table_cell_line_guides(result: OcrResult) -> LineGuides:
    """Build exact restoration guides from OCR cell rectangles."""

    x_values: list[float] = []
    y_values: list[float] = []
    for cells in result.table_cells:
        for cell in cells:
            if cell.right > cell.left and cell.bottom > cell.top:
                x_values.extend((float(cell.left), float(cell.right)))
                y_values.extend((float(cell.top), float(cell.bottom)))

    def unique(values: list[float]) -> list[float]:
        centres: list[float] = []
        for value in sorted(values):
            if not centres or abs(value - centres[-1]) > 2.0:
                centres.append(value)
            else:
                centres[-1] = (centres[-1] + value) / 2.0
        return centres

    return LineGuides(unique(x_values), unique(y_values))


def _word_overlaps_table(
    word: PositionedWord,
    table_bounds: list[tuple[float, float, float, float]],
) -> bool:
    """Return whether a positioned word is inside a detected table."""

    word_left = float(word.left)
    word_top = float(word.top)
    word_right = word_left + float(word.width)
    word_bottom = word_top + float(word.height)
    return any(
        word_right > left
        and word_left < right
        and word_bottom > top
        and word_top < bottom
        for left, top, right, bottom in table_bounds
    )


def _inpaint_table_cell_text(
    canvas: Image.Image,
    cell: TableCell,
) -> None:
    """Remove dark cell text while retaining the paper texture around it."""
    margin = 3
    left = max(int(round(cell.left)) + margin, 0)
    top = max(int(round(cell.top)) + margin, 0)
    right = min(int(round(cell.right)) - margin, canvas.width)
    bottom = min(int(round(cell.bottom)) - margin, canvas.height)
    if right <= left or bottom <= top:
        return

    fallback_color = sample_paper_color(
        canvas,
        (float(cell.left), float(cell.top), float(cell.right), float(cell.bottom)),
    ) or "#ffffff"

    def use_fallback_color() -> None:
        ImageDraw.Draw(canvas).rectangle(
            (left, top, right - 1, bottom - 1),
            fill=fallback_color,
        )

    crop = canvas.crop((left, top, right, bottom))
    try:
        import cv2
        import numpy as np

        rgb = np.asarray(crop)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        block_size = min(31, min(right - left, bottom - top))
        if block_size % 2 == 0:
            block_size -= 1
        if block_size < 3:
            use_fallback_color()
            return

        adaptive_ink = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            block_size,
            9,
        )
        dark_ink = cv2.threshold(gray, 245, 255, cv2.THRESH_BINARY_INV)[1]
        mask = cv2.bitwise_and(adaptive_ink, dark_ink)
        mask = cv2.dilate(mask, np.ones((3, 3), dtype=np.uint8), iterations=1)
        if cv2.countNonZero(mask) == 0:
            return
        repaired = cv2.inpaint(rgb, mask, 3, cv2.INPAINT_TELEA)
        repaired_image = Image.fromarray(repaired)
        try:
            canvas.paste(repaired_image, (left, top))
        finally:
            repaired_image.close()
    except ImportError:
        use_fallback_color()
    except Exception:
        # A malformed or unsupported crop should not make an otherwise valid
        # editable document fail; use the sampled paper tone as a fallback.
        use_fallback_color()
    finally:
        crop.close()


def _positioned_word_center_is_in_cell(word: PositionedWord, cell: TableCell) -> bool:
    center_x = float(word.left) + float(word.width) / 2.0
    center_y = float(word.top) + float(word.height) / 2.0
    return cell.left <= center_x <= cell.right and cell.top <= center_y <= cell.bottom


def _positioned_word_erase_bounds(
    canvas: Image.Image,
    word: PositionedWord,
) -> tuple[int, int, int, int]:
    text_left = max(int(round(word.left)), 0)
    text_top = max(int(round(word.top)), 0)
    text_right = min(int(round(word.left + word.width)), canvas.width)
    text_bottom = min(int(round(word.top + word.height)), canvas.height)
    horizontal_padding = max(1, min(3, int(round(word.height * 0.08))))
    top_padding = max(2, min(5, int(round(word.height * 0.12))))
    bottom_padding = max(1, min(2, int(round(word.height * 0.05))))
    return (
        max(text_left - horizontal_padding, 0),
        max(text_top - top_padding, 0),
        min(text_right + horizontal_padding, canvas.width),
        min(text_bottom + bottom_padding, canvas.height),
    )


def _inpaint_positioned_word(
    canvas: Image.Image,
    original: Image.Image,
    word: PositionedWord,
) -> None:
    left, top, right, bottom = _positioned_word_erase_bounds(canvas, word)
    if right > left and bottom > top:
        _inpaint_positioned_region(canvas, original, left, top, right, bottom)


def _clear_table_cell_text(
    canvas: Image.Image,
    original: Image.Image,
    cell: TableCell,
    words: list[PositionedWord],
    words_by_cell: dict[int, list[PositionedWord]] | None = None,
) -> None:
    """Erase recognized words in a cell without removing nearby form marks."""
    cell_words = (
        words_by_cell.get(id(cell), [])
        if words_by_cell is not None
        else [
            word
            for word in words
            if clean_ocr_text(word.text) and _positioned_word_center_is_in_cell(word, cell)
        ]
    )
    if cell_words:
        erased_bounds: set[tuple[int, int, int, int]] = set()
        for word in cell_words:
            bounds = _positioned_word_erase_bounds(canvas, word)
            if bounds in erased_bounds:
                continue
            erased_bounds.add(bounds)
            left, top, right, bottom = bounds
            if right > left and bottom > top:
                _inpaint_positioned_region(canvas, original, left, top, right, bottom)
        return
    # Some table OCR responses provide cell text without per-word geometry.
    # Keep the previous cell-level fallback for those cells so the native
    # editable text does not sit on top of the same source text.
    if clean_ocr_text(cell.text):
        _inpaint_table_cell_text(canvas, cell)


def _redraw_table_cell_lines(canvas: Image.Image, result: OcrResult) -> None:
    """Draw table borders from geometry without copying source text pixels."""

    draw = ImageDraw.Draw(canvas)
    seen: set[tuple[str, int, int, int]] = set()
    line_color = (88, 88, 88)
    for cells in result.table_cells:
        for cell in cells:
            left = max(int(round(cell.left)), 0)
            top = max(int(round(cell.top)), 0)
            right = min(int(round(cell.right)), canvas.width - 1)
            bottom = min(int(round(cell.bottom)), canvas.height - 1)
            if right <= left or bottom <= top:
                continue
            segments = (
                ("v", left, top, bottom),
                ("v", right, top, bottom),
                ("h", top, left, right),
                ("h", bottom, left, right),
            )
            for direction, position, start, end in segments:
                key = (direction, position, start, end)
                if key in seen:
                    continue
                seen.add(key)
                if direction == "v":
                    draw.line((position, start, position, end), fill=line_color, width=1)
                else:
                    draw.line((start, position, end, position), fill=line_color, width=1)


def _erase_positioned_text(
    result: OcrResult,
    source_image_path: Path,
    output_path: Path,
    extra_words: list | None = None,
    words_to_erase: list[PositionedWord] | None = None,
    clear_cells: list[TableCell] | None = None,
) -> None:
    """Remove OCR regions while retaining scanned table lines.

    ``extra_words`` contains the previous coordinates after an edit. It is
    needed when a replacement box moved or became smaller than the old scan
    glyphs; otherwise a fragment of the original text can remain underneath
    the new editable text box.
    """

    with Image.open(source_image_path) as source:
        original = _image_on_white(source)
        canvas = original.copy()
    detected_line_guides = _table_cell_line_guides(result)
    if not (detected_line_guides.x or detected_line_guides.y):
        try:
            detected_line_guides = detect_table_line_guides(
                source_image_path,
                (0.0, 0.0, float(canvas.width), float(canvas.height)),
            )
        except Exception:
            detected_line_guides = LineGuides([], [])
    table_bounds = _table_bounds(result)
    # Clear only recognized text regions in changed cells. Form marks such as
    # checkboxes and handwritten strokes outside the OCR boxes belong to the
    # source and should remain visible after an edit.
    cells_to_clear = (
        [cell for cells in result.table_cells for cell in cells]
        if clear_cells is None
        else list(clear_cells)
    )
    erase_words = list(words_to_erase) if words_to_erase is not None else list(result.positioned_words)
    if words_to_erase is None and extra_words:
        erase_words.extend(extra_words)
    words_by_cell = _positioned_words_by_table_cell(result.table_cells, erase_words)
    cells_to_clear_index = _table_cell_coordinate_index(
        [cells_to_clear],
        include_degenerate=True,
    )
    for cell in cells_to_clear:
        _clear_table_cell_text(canvas, original, cell, erase_words, words_by_cell)
    for word in erase_words:
        if not clean_ocr_text(word.text):
            continue
        left, top, right, bottom = _positioned_word_erase_bounds(canvas, word)
        if right <= left or bottom <= top:
            continue
        if _table_contains_center(
            float(word.left) + float(word.width) / 2.0,
            float(word.top) + float(word.height) / 2.0,
            cells_to_clear_index,
        ):
            continue
        _inpaint_positioned_region(canvas, original, left, top, right, bottom)
        _restore_table_lines(
            canvas,
            original,
            detected_line_guides,
            left,
            top,
            right,
            bottom,
            table_bounds,
            result.table_cells,
        )
    if result.table_cells:
        _redraw_table_cell_lines(canvas, result)
    # Restore only line segments that correspond to cell edges. Reusing global
    # x/y guides across rows can paste character strokes back into edited cells.
    if table_bounds:
        _restore_table_lines(
            canvas,
            original,
            detected_line_guides,
            0,
            0,
            canvas.width,
            canvas.height,
            table_bounds,
            result.table_cells,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Edited PDF pages use the same lossless intermediate as the visual Word
    # background. This avoids JPEG halos around repaired text and thin rules.
    canvas.save(output_path, format="PNG", optimize=True)


def _inpaint_positioned_region(
    canvas: Image.Image,
    original: Image.Image,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> None:
    """Remove a changed scan word while matching the nearby paper background."""

    try:
        import cv2
        import numpy as np
    except ImportError:
        paper_color = sample_paper_color(
            original,
            (left, top, right, bottom),
        ) or "#ffffff"
        ImageDraw.Draw(canvas).rectangle(
            (left, top, right - 1, bottom - 1),
            fill=paper_color,
        )
        return

    padding = max(5, min(24, int(round((bottom - top) * 0.65))))
    crop_left = max(left - padding, 0)
    crop_top = max(top - padding, 0)
    crop_right = min(right + padding, original.width)
    crop_bottom = min(bottom + padding, original.height)
    if crop_right <= crop_left or crop_bottom <= crop_top:
        return
    # Use the in-progress canvas so adjacent OCR boxes don't restore a word
    # that an earlier inpaint operation already removed.
    rgb = np.asarray(canvas.crop((crop_left, crop_top, crop_right, crop_bottom)))
    mask = np.zeros(rgb.shape[:2], dtype=np.uint8)
    mask_top = max(top - crop_top, 0)
    mask_left = max(left - crop_left, 0)
    mask_bottom = min(bottom - crop_top, mask.shape[0])
    mask_right = min(right - crop_left, mask.shape[1])
    if mask_bottom <= mask_top or mask_right <= mask_left:
        return
    mask[mask_top:mask_bottom, mask_left:mask_right] = 255
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    repaired = cv2.inpaint(
        bgr,
        mask,
        max(2.0, min(5.0, (bottom - top) * 0.18)),
        cv2.INPAINT_TELEA,
    )
    repaired_rgb = cv2.cvtColor(repaired, cv2.COLOR_BGR2RGB)
    canvas.paste(Image.fromarray(repaired_rgb), (crop_left, crop_top))


def _fill_paper_region(
    canvas: Image.Image,
    original: Image.Image,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> None:
    """Replace OCR pixels with nearby paper instead of smearing them by inpainting."""
    if right <= left or bottom <= top:
        return
    paper_color = sample_paper_color(
        original,
        (float(left), float(top), float(right), float(bottom)),
    ) or "#ffffff"
    ImageDraw.Draw(canvas).rectangle(
        (left, top, right - 1, bottom - 1),
        fill=paper_color,
    )


def _clear_hybrid_table_cell_text(
    canvas: Image.Image,
    original: Image.Image,
    cell: TableCell,
    words: list[PositionedWord],
    *,
    clear_all_ink: bool = False,
) -> None:
    """Clear recognized cell content before rebuilding it as editable Word text."""
    if clear_all_ink:
        # The structured export rebuilds every table glyph as editable text.
        # Inpaint all cell ink first so missed OCR boxes cannot leave shadows.
        _inpaint_table_cell_text(canvas, cell)
        return
    # Clear only OCR text bounds so scanned paper texture and form marks stay
    # intact. Table rules are redrawn after every cell has been processed.
    _clear_table_cell_text(canvas, original, cell, words)


def _sample_cell_paper_color(
    image: Image.Image,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> str:
    """Sample a cell's own light paper fill from its corners, away from neighbors."""
    width = right - left
    height = bottom - top
    patch_size = max(2, min(8, int(round(min(width, height) * 0.08))))
    inset = max(3, min(6, int(round(min(width, height) * 0.05))))
    if width <= inset * 2 + patch_size or height <= inset * 2 + patch_size:
        return sample_paper_color(
            image,
            (float(left), float(top), float(right), float(bottom)),
        ) or "#ffffff"

    boxes = (
        (left + inset, top + inset, left + inset + patch_size, top + inset + patch_size),
        (right - inset - patch_size, top + inset, right - inset, top + inset + patch_size),
        (left + inset, bottom - inset - patch_size, left + inset + patch_size, bottom - inset),
        (right - inset - patch_size, bottom - inset - patch_size, right - inset, bottom - inset),
    )
    channels: list[list[int]] = [[], [], []]
    for box in boxes:
        source_crop = image.crop(box)
        try:
            crop = source_crop.convert("RGB")
            try:
                for red, green, blue in crop.getdata():
                    if min(red, green, blue) < 180:
                        continue
                    channels[0].append(red)
                    channels[1].append(green)
                    channels[2].append(blue)
            finally:
                crop.close()
        finally:
            source_crop.close()
    if not channels[0]:
        return sample_paper_color(
            image,
            (float(left), float(top), float(right), float(bottom)),
        ) or "#ffffff"
    color = [sorted(channel)[len(channel) // 2] for channel in channels]
    return "#" + "".join(f"{value:02x}" for value in color)


def _same_positioned_word(left: PositionedWord, right: PositionedWord) -> bool:
    """Return whether two OCR overlays represent the same unchanged word."""

    return (
        clean_ocr_text(left.text) == clean_ocr_text(right.text)
        and abs(float(left.left) - float(right.left)) <= 1.5
        and abs(float(left.top) - float(right.top)) <= 1.5
        and abs(float(left.width) - float(right.width)) <= 2.0
        and abs(float(left.height) - float(right.height)) <= 2.0
    )


def _has_same_positioned_word(
    word: PositionedWord,
    candidates: list[PositionedWord],
    index: dict[tuple[int, int], list[int]],
) -> bool:
    return any(
        _same_positioned_word(word, candidates[candidate_index])
        for candidate_index in _coordinate_word_candidates(word, index)
    )


def _has_positioned_word_changes(
    original_words: list[PositionedWord],
    edited_words: list[PositionedWord],
) -> bool:
    """Match the legacy overlay-change check using one spatial index."""

    if len(original_words) != len(edited_words):
        return True
    original_index = _coordinate_word_index(original_words)
    return any(
        not _has_same_positioned_word(word, original_words, original_index)
        for word in edited_words
    )


def _restore_table_lines(
    canvas: Image.Image,
    original: Image.Image,
    guides: LineGuides,
    left: int,
    top: int,
    right: int,
    bottom: int,
    table_bounds: list[tuple[float, float, float, float]] | None = None,
    table_cells: list[list[TableCell]] | None = None,
) -> None:
    for x_value in guides.x:
        x = int(round(x_value))
        if not (left <= x < right):
            continue
        if table_bounds and not _line_overlaps_table(
            "vertical", x, top, bottom, table_bounds
        ):
            continue
        strip_left = max(x - 1, 0)
        strip_right = min(x + 2, canvas.width)
        segments = (
            [
                (
                    max(top, int(round(cell.top)), 0),
                    min(bottom, int(round(cell.bottom)), canvas.height),
                )
                for cells in table_cells
                for cell in cells
                if any(abs(float(x) - edge) <= 2.0 for edge in (cell.left, cell.right))
            ]
            if table_cells
            else [(top, bottom)]
        )
        for segment_top, segment_bottom in segments:
            if segment_bottom <= segment_top:
                continue
            canvas.paste(
                original.crop((strip_left, segment_top, strip_right, segment_bottom)),
                (strip_left, segment_top),
            )
    for y_value in guides.y:
        y = int(round(y_value))
        if not (top <= y < bottom):
            continue
        if table_bounds and not _line_overlaps_table(
            "horizontal", y, left, right, table_bounds
        ):
            continue
        strip_top = max(y - 1, 0)
        strip_bottom = min(y + 2, canvas.height)
        segments = (
            [
                (
                    max(left, int(round(cell.left)), 0),
                    min(right, int(round(cell.right)), canvas.width),
                )
                for cells in table_cells
                for cell in cells
                if any(abs(float(y) - edge) <= 2.0 for edge in (cell.top, cell.bottom))
            ]
            if table_cells
            else [(left, right)]
        )
        for segment_left, segment_right in segments:
            if segment_right <= segment_left:
                continue
            canvas.paste(
                original.crop((segment_left, strip_top, segment_right, strip_bottom)),
                (segment_left, strip_top),
            )


def _table_bounds(result: OcrResult) -> list[tuple[float, float, float, float]]:
    """Return the real table extents used to scope line restoration."""

    bounds: list[tuple[float, float, float, float]] = []
    for cells in result.table_cells:
        valid = [
            cell
            for cell in cells
            if cell.right > cell.left and cell.bottom > cell.top
        ]
        if not valid:
            continue
        bounds.append(
            (
                min(cell.left for cell in valid),
                min(cell.top for cell in valid),
                max(cell.right for cell in valid),
                max(cell.bottom for cell in valid),
            )
        )
    return bounds


def _line_overlaps_table(
    orientation: str,
    line: int,
    start: int,
    end: int,
    table_bounds: list[tuple[float, float, float, float]],
) -> bool:
    """Check that a detected guide crosses a table, not just its text area."""

    for left, top, right, bottom in table_bounds:
        if orientation == "vertical":
            if left - 1 <= line <= right + 1 and max(start, top) < min(end, bottom):
                return True
        elif top - 1 <= line <= bottom + 1 and max(start, left) < min(end, right):
            return True
    return False


def _render_font_path() -> str | None:
    candidates = [
        os.getenv("OCR_RENDER_FONT", "").strip(),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _fit_render_text(draw, text: str, max_width: int, max_height: int, font_path: str | None):
    max_width = max(max_width, 2)
    max_height = max(max_height, 8)
    initial_size = max(8, min(48, int(max_height * 0.78)))
    for size in range(initial_size, 5, -1):
        try:
            font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        except OSError:
            font = ImageFont.load_default()
        lines: list[str] = []
        for paragraph in text.splitlines() or [text]:
            current = ""
            for character in paragraph or " ":
                candidate = current + character
                bbox = draw.textbbox((0, 0), candidate, font=font)
                if current and bbox[2] - bbox[0] > max_width:
                    lines.append(current)
                    current = character
                else:
                    current = candidate
            lines.append(current)
        line_height = max(int(size * 1.08), 1)
        if len(lines) * line_height <= max_height or size <= 6:
            return font, lines
    return ImageFont.load_default(), [text]


def _append_drawing_image(
    paragraph,
    image_path: Path,
    width_pt: float,
    height_pt: float,
    *,
    print_resolution_limit: bool = True,
) -> None:
    run = paragraph.add_run()
    with Image.open(image_path) as source:
        target_size = (
            _print_export_image_size(
                source.width,
                source.height,
                width_pt,
                height_pt,
            )
            if print_resolution_limit
            else source.size
        )
        if target_size == source.size:
            run.add_picture(str(image_path), width=Pt(width_pt), height=Pt(height_pt))
        else:
            prepared = _image_on_white(source)
            try:
                resized = prepared.resize(target_size, Image.Resampling.LANCZOS)
                try:
                    image_bytes = io.BytesIO()
                    image_format = (
                        "JPEG"
                        if source.format in {"JPEG", "MPO"}
                        and resized.mode in {"RGB", "L"}
                        else "PNG"
                    )
                    save_options: dict[str, object]
                    if image_format == "JPEG":
                        save_options = {"quality": 95, "optimize": True}
                    else:
                        save_options = {"compress_level": 1}
                    icc_profile = source.info.get("icc_profile")
                    if (
                        isinstance(icc_profile, bytes)
                        and source.mode == resized.mode
                    ):
                        save_options["icc_profile"] = icc_profile
                    resized.save(image_bytes, format=image_format, **save_options)
                    image_bytes.seek(0)
                    run.add_picture(
                        image_bytes,
                        width=Pt(width_pt),
                        height=Pt(height_pt),
                    )
                finally:
                    resized.close()
                    image_bytes.close()
            finally:
                prepared.close()
    drawing = run._r.find(qn("w:drawing"))
    inline = drawing.find(qn("wp:inline")) if drawing is not None else None
    if drawing is None or inline is None:
        raise RuntimeError("无法创建原图 Word 图层")
    graphic = inline.find(qn("a:graphic"))
    if graphic is None:
        raise RuntimeError("原图 Word 图层缺少图片数据")
    inline.remove(graphic)
    drawing.remove(inline)
    drawing.append(_make_anchor(graphic, width_pt, height_pt, 0, 0, 1, "scan-page-background", behind=True))


def _append_drawing_textbox(
    paragraph,
    text: str,
    left_pt: float,
    top_pt: float,
    width_pt: float,
    height_pt: float,
    font_size_pt: float,
    index: int,
    *,
    transparent: bool = False,
    horizontal_alignment: str = "left",
    bold: bool = False,
    font_color: str = "000000",
) -> None:
    run = OxmlElement("w:r")
    graphic = etree.Element(f"{{{A_NS}}}graphic")
    graphic_data = etree.SubElement(
        graphic,
        f"{{{A_NS}}}graphicData",
        uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    )
    shape = etree.SubElement(graphic_data, f"{{{WPS_NS}}}wsp")
    etree.SubElement(shape, f"{{{WPS_NS}}}cNvSpPr", txBox="1")
    shape_properties = etree.SubElement(shape, f"{{{WPS_NS}}}spPr")
    transform = etree.SubElement(shape_properties, f"{{{A_NS}}}xfrm")
    etree.SubElement(transform, f"{{{A_NS}}}off", x="0", y="0")
    etree.SubElement(
        transform,
        f"{{{A_NS}}}ext",
        cx=str(max(int(round(width_pt * EMU_PER_PT)), 1)),
        cy=str(max(int(round(height_pt * EMU_PER_PT)), 1)),
    )
    geometry = etree.SubElement(shape_properties, f"{{{A_NS}}}prstGeom", prst="rect")
    etree.SubElement(geometry, f"{{{A_NS}}}avLst")
    if transparent:
        etree.SubElement(shape_properties, f"{{{A_NS}}}noFill")
    else:
        solid_fill = etree.SubElement(shape_properties, f"{{{A_NS}}}solidFill")
        etree.SubElement(solid_fill, f"{{{A_NS}}}srgbClr", val="FFFFFF")
    line = etree.SubElement(shape_properties, f"{{{A_NS}}}ln")
    etree.SubElement(line, f"{{{A_NS}}}noFill")

    textbox = etree.SubElement(shape, f"{{{WPS_NS}}}txbx")
    content = OxmlElement("w:txbxContent")
    text_paragraph = OxmlElement("w:p")
    paragraph_properties = OxmlElement("w:pPr")
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:before"), "0")
    spacing.set(qn("w:after"), "0")
    # ``w:line`` is measured in twentieths of a point. A value of ``1``
    # makes newline-separated vertical labels collapse into a black mark in
    # Word/LibreOffice even though the textbox itself has the right height.
    spacing.set(
        qn("w:line"),
        str(max(int(round(font_size_pt * 20 * 1.05)), 120)),
    )
    spacing.set(qn("w:lineRule"), "exact")
    paragraph_properties.append(spacing)
    if horizontal_alignment == "center":
        alignment = OxmlElement("w:jc")
        alignment.set(qn("w:val"), "center")
        paragraph_properties.append(alignment)
    text_paragraph.append(paragraph_properties)
    text_run = OxmlElement("w:r")
    run_properties = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    for key in ("ascii", "hAnsi", "eastAsia", "cs"):
        fonts.set(qn(f"w:{key}"), "Microsoft YaHei")
    run_properties.append(fonts)
    if bold:
        run_properties.append(OxmlElement("w:b"))
    color = OxmlElement("w:color")
    color.set(
        qn("w:val"),
        font_color.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", str(font_color)) else "000000",
    )
    run_properties.append(color)
    size = OxmlElement("w:sz")
    size.set(qn("w:val"), str(max(int(round(font_size_pt * 2)), 8)))
    run_properties.append(size)
    text_run.append(run_properties)
    text_node = OxmlElement("w:t")
    text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text_node.text = text or " "
    text_run.append(text_node)
    text_paragraph.append(text_run)
    content.append(text_paragraph)
    textbox.append(content)
    body_properties = etree.SubElement(
        shape,
        f"{{{WPS_NS}}}bodyPr",
        rot="0",
        spcFirstLastPara="0",
        vertOverflow="clip",
        horzOverflow="clip",
        vert="horz",
        wrap="square",
        lIns="0",
        tIns="0",
        rIns="0",
        bIns="0",
    )
    # Let Word/WPS reduce the run before clipping it when OCR text is a little
    # wider than the source rectangle. This is especially important for
    # Chinese form rows where the scan font and Microsoft YaHei have different
    # glyph widths.
    etree.SubElement(
        body_properties,
        f"{{{A_NS}}}normAutofit",
        fontScale="70000",
        lnSpcReduction="20000",
    )
    # A floating DrawingML object must be wrapped in w:drawing inside the
    # run. Appending wp:anchor directly to w:r produces XML that python-docx
    # may still save, but Word/WPS can silently drop or repair the editable
    # text box when opening the file.
    drawing = OxmlElement("w:drawing")
    drawing.append(
        _make_anchor(
            graphic,
            width_pt,
            height_pt,
            left_pt,
            top_pt,
            index + 2,
            f"ocr-text-{index}",
            behind=False,
        )
    )
    run.append(drawing)
    paragraph._p.append(run)


def _editable_textbox_font_size(text: str, width_pt: float, height_pt: float) -> float:
    """Fit OCR text inside its source box when rendered by Word fonts."""

    lines = text.splitlines() or [text]
    longest_line = max(
        (
            sum(1.0 if ord(character) >= 0x2E80 else 0.55 for character in line)
            for line in lines
        ),
        default=1.0,
    )
    # Microsoft YaHei is wider than many printed scan fonts. Leave a small
    # safety margin so Word/WPS does not clip the last Chinese character.
    width_size = width_pt / max(longest_line * 1.3, 1.0)
    height_size = height_pt * 0.72
    return max(min(width_size, height_size), 4.0)


def _make_anchor(
    graphic,
    width_pt: float,
    height_pt: float,
    left_pt: float,
    top_pt: float,
    document_id: int,
    name: str,
    *,
    behind: bool,
):
    width_emu = max(int(round(width_pt * EMU_PER_PT)), 1)
    height_emu = max(int(round(height_pt * EMU_PER_PT)), 1)
    anchor = etree.Element(
        f"{{{WP_NS}}}anchor",
        distT="0",
        distB="0",
        distL="0",
        distR="0",
        simplePos="0",
        relativeHeight="0" if behind else "2",
        behindDoc="1" if behind else "0",
        locked="0",
        layoutInCell="1",
        allowOverlap="1",
    )
    etree.SubElement(anchor, f"{{{WP_NS}}}simplePos", x="0", y="0")
    position_h = etree.SubElement(anchor, f"{{{WP_NS}}}positionH", relativeFrom="page")
    etree.SubElement(position_h, f"{{{WP_NS}}}posOffset").text = str(int(round(left_pt * EMU_PER_PT)))
    position_v = etree.SubElement(anchor, f"{{{WP_NS}}}positionV", relativeFrom="page")
    etree.SubElement(position_v, f"{{{WP_NS}}}posOffset").text = str(int(round(top_pt * EMU_PER_PT)))
    etree.SubElement(anchor, f"{{{WP_NS}}}extent", cx=str(width_emu), cy=str(height_emu))
    etree.SubElement(anchor, f"{{{WP_NS}}}effectExtent", l="0", t="0", r="0", b="0")
    etree.SubElement(anchor, f"{{{WP_NS}}}wrapNone")
    etree.SubElement(anchor, f"{{{WP_NS}}}docPr", id=str(document_id), name=name)
    frame = etree.SubElement(anchor, f"{{{WP_NS}}}cNvGraphicFramePr")
    etree.SubElement(frame, f"{{{A_NS}}}graphicFrameLocks", noChangeAspect="1")
    anchor.append(graphic)
    return anchor


def _column_widths(layout: TableLayout, count: int, total_width: int) -> list[int]:
    if count <= 0:
        return []
    source_widths: list[float] = []
    for raw_width in layout.column_widths:
        try:
            width = float(raw_width)
        except (TypeError, ValueError, OverflowError):
            width = 0.0
        source_widths.append(width if isfinite(width) and width > 0 else 0.0)
    if len(source_widths) != count or not any(source_widths):
        return [int(total_width / count)] * count
    positive_widths = [width for width in source_widths if width > 0]
    width_scale = max(positive_widths)
    normalized_widths = [
        width / width_scale if width > 0 else 0.0
        for width in source_widths
    ]
    positive_normalized = [width for width in normalized_widths if width > 0]
    fallback_width = sum(positive_normalized) / len(positive_normalized)
    normalized_widths = [
        width if width > 0 else fallback_width
        for width in normalized_widths
    ]
    source_total = sum(normalized_widths)
    widths = [int(total_width * width / source_total) for width in normalized_widths]
    widths[-1] += total_width - sum(widths)
    return widths


def _native_merge_span(
    cell: TableCell,
    row_count: int,
    column_count: int,
) -> tuple[int, int, int, int] | None:
    """Clamp an OCR merge span to the actual Word table grid."""

    row_start = max(cell.row_start, 0)
    row_end = min(cell.row_end, row_count)
    col_start = max(cell.col_start, 0)
    col_end = min(cell.col_end, column_count)
    if row_end <= row_start or col_end <= col_start:
        return None
    return row_start, row_end, col_start, col_end


def _native_merge_plan(
    cells: list[TableCell],
    row_count: int,
    column_count: int,
) -> list[TableCell]:
    """Return a non-overlapping merge plan for a native Word table.

    OCR table APIs can describe a form as overlapping semantic regions. For
    example, one result may report a large right-hand area spanning several
    rows while another reports a label column crossing part of that area. A
    Word tables can only merge rectangular cells in a single partition;
    trying to apply overlapping spans raises ``InvalidSpanError`` and can
    abort the structured document export.

    Prefer the largest valid regions and skip only conflicting regions. The
    original-layout Word remains a separate scan-backed file, so this fallback
    affects only the editable native-table variant.
    """

    candidates: list[tuple[int, int, int, int, int, int, int, TableCell]] = []
    for cell in cells:
        span = _native_merge_span(cell, row_count, column_count)
        if span is None:
            continue
        row_start, row_end, col_start, col_end = span
        height = row_end - row_start
        width = col_end - col_start
        if height <= 1 and width <= 1:
            continue
        candidates.append(
            (
                height * width,
                height,
                width,
                row_start,
                row_end,
                col_start,
                col_end,
                cell,
            )
        )

    # Larger regions carry more of the form's structural intent. Stable
    # tie-breakers keep the result deterministic when OCR returns duplicates.
    candidates.sort(
        key=lambda item: (
            -item[0],
            -item[1],
            -item[2],
            item[3],
            item[5],
            item[4],
            item[6],
        )
    )
    occupied_by_row: dict[int, list[tuple[int, int]]] = {}
    accepted: list[TableCell] = []
    for (
        _area,
        _height,
        _width,
        row_start,
        row_end,
        col_start,
        col_end,
        cell,
    ) in candidates:
        overlaps = False
        for row in range(row_start, row_end):
            intervals = occupied_by_row.get(row, ())
            insertion_index = bisect_left(intervals, (col_start, col_end))
            if (
                insertion_index > 0
                and intervals[insertion_index - 1][1] > col_start
            ) or (
                insertion_index < len(intervals)
                and intervals[insertion_index][0] < col_end
            ):
                overlaps = True
                break
        if overlaps:
            continue
        for row in range(row_start, row_end):
            intervals = occupied_by_row.setdefault(row, [])
            insort(intervals, (col_start, col_end))
        accepted.append(cell)
    return accepted


def _table_target_width(layout: TableLayout, usable_width: int) -> int:
    if (
        isfinite(layout.source_width)
        and isfinite(layout.source_left)
        and isfinite(layout.source_right)
        and layout.source_width > 0
        and layout.source_right > layout.source_left
    ):
        ratio = (layout.source_right - layout.source_left) / layout.source_width
        return max(int(usable_width * ratio), Cm(5))
    return usable_width


def _native_side_margin(result: OcrResult):
    """Choose a side margin that does not clip edge-to-edge source tables.

    The source coordinates are measured from the whole scan. A competition
    result table can occupy almost the entire page, while a form table usually
    starts well inside the page. A fixed margin makes the former extend past
    the Word page and hides its final columns, so only edge-to-edge tables use
    a narrow margin.
    """

    return Cm(0.15) if _has_edge_to_edge_table(result) else Cm(1.15)


def _has_edge_to_edge_table(result: OcrResult) -> bool:
    for layout in result.table_layouts:
        if (
            not isfinite(layout.source_width)
            or not isfinite(layout.source_left)
            or not isfinite(layout.source_right)
            or layout.source_width <= 0
            or layout.source_right <= layout.source_left
        ):
            continue
        left_ratio = layout.source_left / layout.source_width
        right_ratio = layout.source_right / layout.source_width
        if left_ratio <= 0.04 and right_ratio >= 0.96:
            return True
    return False


def _table_target_left(layout: TableLayout, usable_width: int) -> int:
    if (
        isfinite(layout.source_width)
        and isfinite(layout.source_left)
        and layout.source_width > 0
        and layout.source_left > 0
    ):
        return max(int(usable_width * layout.source_left / layout.source_width), 0)
    return 0


def _paragraph_blocks(result: OcrResult) -> list[tuple[str, str, ParagraphLayout | None]]:
    styles = list(result.paragraph_styles)
    if len(styles) < len(result.paragraphs):
        styles.extend(["body"] * (len(result.paragraphs) - len(styles)))
    layouts = list(result.paragraph_layouts)
    if len(layouts) < len(result.paragraphs):
        layouts.extend([None] * (len(result.paragraphs) - len(layouts)))
    blocks: list[tuple[str, str, ParagraphLayout | None]] = []
    for index, text in enumerate(result.paragraphs):
        style = styles[index] if index < len(styles) else "body"
        if style not in {"title", "subtitle", "body"}:
            style = "body"
        # Backward compatibility for metadata created before paragraph styles
        # were stored. Do not apply this heuristic to text-only documents.
        layout = layouts[index] if index < len(layouts) else None
        if (
            style == "body"
            and result.tables
            and (layout is None or result.image_height <= 0 or layout.top <= result.image_height * 0.3)
            and ("申请表" in text or "比赛结果" in text)
        ):
            style = "title"
        blocks.append((text, style, layout))
    return blocks


def _ocr_heading_font_size_pt(
    result: OcrResult,
    text: str,
    page_width_pt: float,
    *,
    scale: float = 0.75,
) -> float | None:
    """Estimate scanned form heading size from its positioned OCR box."""
    normalized_text = "".join(clean_ocr_text(text).split())
    if not normalized_text or result.image_width <= 0 or page_width_pt <= 0:
        return None
    matching_heights = [
        float(word.height)
        for word in result.positioned_words
        if "".join(clean_ocr_text(word.text).split()) == normalized_text
        and isfinite(float(word.height))
        and float(word.height) > 0
    ]
    if not matching_heights:
        return None
    source_height_pt = median(matching_heights) * page_width_pt / result.image_width
    return max(6.0, min(18.0, source_height_pt * scale))


def _ocr_heading_gap_pt(
    result: OcrResult,
    text: str,
    next_text: str,
    page_width_pt: float,
) -> float:
    """Preserve the scanned vertical gap between the title and subtitle."""
    normalized_text = "".join(clean_ocr_text(text).split())
    normalized_next = "".join(clean_ocr_text(next_text).split())
    if (
        not normalized_text
        or not normalized_next
        or result.image_width <= 0
        or page_width_pt <= 0
    ):
        return 0.0
    current = [
        word
        for word in result.positioned_words
        if "".join(clean_ocr_text(word.text).split()) == normalized_text
        and isfinite(float(word.top))
        and isfinite(float(word.height))
        and float(word.height) > 0
    ]
    following = [
        word
        for word in result.positioned_words
        if "".join(clean_ocr_text(word.text).split()) == normalized_next
        and isfinite(float(word.top))
        and isfinite(float(word.height))
        and float(word.height) > 0
    ]
    if not current or not following:
        return 0.0
    source_gap = min(float(word.top) for word in following) - max(
        float(word.top) + float(word.height) for word in current
    )
    return max(0.0, min(12.0, source_gap * page_width_pt / result.image_width))


def _add_positioned_line(
    paragraph,
    layout: ParagraphLayout,
    positioned_words: Iterable[PositionedWord] | None = None,
    paragraph_origin_pt: float = 0,
    page_width_pt: float = 595.276,
) -> None:
    """Keep form labels, entered values, and underline fields in the Word export."""

    fragments: list[tuple[float, float, str, bool]] = [
        (fragment.left, fragment.width, clean_ocr_text(fragment.text), False)
        for fragment in layout.fragments
    ]
    fragment_text = "".join(text for _left, _width, text, _is_form_field in fragments)
    line_height = max(layout.bottom - layout.top, 1.0)
    vertical_tolerance = max(2.0, line_height * 0.35)
    for word in positioned_words or ():
        text = clean_ocr_text(word.text)
        if not word.is_form_field or not text or text in fragment_text:
            continue
        if (
            word.top + word.height < layout.top - vertical_tolerance
            or word.top > layout.bottom + vertical_tolerance
            or word.left + word.width <= layout.left
            or word.left >= layout.right
        ):
            continue
        fragments.append((word.left, word.width, text, True))
    fragments.sort(key=lambda item: item[0])

    source_width = max(layout.source_width, layout.right, 1.0)
    page_scale = max(page_width_pt, 0.0) / source_width
    paragraph_indent_pt = (
        min((fragment[0] for fragment in fragments), default=layout.left) * page_scale
        - paragraph_origin_pt
    )
    paragraph.paragraph_format.left_indent = Pt(paragraph_indent_pt)
    previous_right = None
    previous_text = ""
    for left, width, text, is_form_field in fragments:
        if previous_right is not None:
            gap = max(left - previous_right, 0.0)
            if gap >= 8:
                if _is_fixed_header_field_gap(previous_text, text):
                    tab_position_pt = (
                        left * page_scale
                        - paragraph_origin_pt
                        - paragraph_indent_pt
                    )
                    if tab_position_pt > 0:
                        paragraph.paragraph_format.tab_stops.add_tab_stop(
                            Pt(tab_position_pt),
                            WD_TAB_ALIGNMENT.LEFT,
                            WD_TAB_LEADER.LINES,
                        )
                        paragraph.add_run("\t")
                    else:
                        filler = paragraph.add_run("_" * max(3, int(gap / 16)))
                        filler.font.name = "Microsoft YaHei"
                        filler.font.size = Pt(11)
                else:
                    filler = paragraph.add_run("_" * max(3, int(gap / 16)))
                    filler.font.name = "Microsoft YaHei"
                    filler.font.size = Pt(11)
        run = paragraph.add_run(text)
        run.font.name = "Microsoft YaHei"
        run.font.size = Pt(11)
        run.underline = is_form_field
        run.font.bold = False
        run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        if is_form_field:
            # Keep the printed line visible below the entered value and carry
            # it through the rest of the same blank field.
            text_units = sum(0.5 if character.isascii() else 1.0 for character in text)
            text_width = text_units * 11 / max(page_scale, 0.001)
            remaining_width = max(width - text_width, 0.0)
            if remaining_width >= 24:
                filler = paragraph.add_run("_" * max(1, int(remaining_width / 16)))
                filler.font.name = "Microsoft YaHei"
                filler.font.size = Pt(11)
            previous_right = max(previous_right or 0.0, left + width)
        else:
            previous_right = max(previous_right or 0.0, left + width)
        previous_text = text
    paragraph_width_pt = max(
        page_width_pt - paragraph_origin_pt * 2.0 - paragraph_indent_pt,
        0.0,
    )
    last_fragment_right_pt = max(
        (previous_right or 0.0) * page_scale
        - paragraph_origin_pt
        - paragraph_indent_pt,
        0.0,
    )
    trailing_gap_pt = max(paragraph_width_pt - last_fragment_right_pt, 0.0)
    if trailing_gap_pt >= 24:
        # LibreOffice wraps a trailing right-tab leader with no following text
        # onto the next line. Fill only most of the measured gap with short
        # underscores so the blank field stays beside its label.
        underline_count = max(1, int(trailing_gap_pt / 6.6))
        filler = paragraph.add_run("_" * underline_count)
        filler.font.name = "Microsoft YaHei"
        filler.font.size = Pt(11)


def _positioned_paragraph_layout(
    result: OcrResult,
    text: str,
) -> ParagraphLayout | None:
    """Recover a missing form-line layout when OCR words match its paragraph."""

    target = "".join(clean_plain_ocr_text(text).split())
    if not target or result.image_width <= 0:
        return None

    table_index = _table_cell_coordinate_index(result.table_cells)
    candidates = []
    for word in result.positioned_words:
        word_text = clean_plain_ocr_text(word.text)
        if not word_text or word.width <= 0 or word.height <= 0:
            continue
        center_x = word.left + word.width / 2.0
        center_y = word.top + word.height / 2.0
        if _table_contains_center(center_x, center_y, table_index):
            continue
        candidates.append(word)

    line_groups: list[list[PositionedWord]] = []
    for word in sorted(
        candidates,
        key=lambda item: (item.top + item.height / 2.0, item.left),
    ):
        center_y = word.top + word.height / 2.0
        matching_group = line_groups[-1] if line_groups else None
        if matching_group is not None:
            group_center_y = sum(
                item.top + item.height / 2.0 for item in matching_group
            ) / len(matching_group)
            line_tolerance = max(
                3.0,
                max(word.height, max(item.height for item in matching_group)) * 0.45,
            )
            if abs(center_y - group_center_y) > line_tolerance:
                matching_group = None
        if matching_group is None:
            line_groups.append([word])
        else:
            matching_group.append(word)

    for group in line_groups:
        ordered = sorted(group, key=lambda item: item.left)
        line_text = "".join(clean_plain_ocr_text(word.text) for word in ordered)
        if "".join(line_text.split()) != target:
            continue
        fragments = [
            TextFragment(
                text=clean_plain_ocr_text(word.text),
                left=word.left,
                top=word.top,
                width=word.width,
                height=word.height,
            )
            for word in ordered
        ]
        return ParagraphLayout(
            fragments=fragments,
            source_width=float(result.image_width),
            left=min(fragment.left for fragment in fragments),
            top=min(fragment.top for fragment in fragments),
            right=max(fragment.left + fragment.width for fragment in fragments),
            bottom=max(fragment.top + fragment.height for fragment in fragments),
        )
    return None


def _is_fixed_header_field_gap(left_text: str, right_text: str) -> bool:
    """Identify large fixed gaps between student-header fields."""
    left_label = re.sub(r"[\s（）()]+", "", left_text)
    right_label = re.sub(r"[\s（）()]+", "", right_text)
    return (
        bool(re.search(r"学校", left_label))
        and bool(re.match(r"年级", right_label))
    ) or (
        bool(re.search(r"年级", left_label))
        and bool(re.match(r"班(?:别|级)", right_label))
    ) or (
        bool(re.search(r"班(?:别|级)", left_label))
        and bool(re.match(r"学(?:籍)?号", right_label))
    )


def _style_cell(
    cell,
    bold: bool,
    *,
    font_size: float = 9.0,
    horizontal_alignment: WD_ALIGN_PARAGRAPH | None = None,
) -> None:
    cell_text = "\n".join(paragraph.text for paragraph in cell.paragraphs)
    alignment = horizontal_alignment
    if alignment is None:
        alignment = (
            WD_ALIGN_PARAGRAPH.LEFT
            if "□" in cell_text or len(cell_text.replace("\n", "")) > 24
            else WD_ALIGN_PARAGRAPH.CENTER
        )
    for cell_paragraph in cell.paragraphs:
        cell_paragraph.alignment = alignment
        cell_paragraph.paragraph_format.space_before = Pt(0)
        cell_paragraph.paragraph_format.space_after = Pt(0)
        cell_paragraph.paragraph_format.line_spacing = 1.0
        for run in cell_paragraph.runs:
            run.font.name = "SimSun"
            run.font.size = Pt(font_size)
            run.font.bold = bold
            run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "SimSun")


def _native_cell_font_size_for_geometry(
    text: str,
    source_cell: TableCell | None,
    row_index: int,
    column_index: int,
    column_widths: list[int],
    row_heights_pt: list[float],
    table_scale: float,
    source_word_heights_pt: Iterable[float],
    source_word_widths_pt: Iterable[float],
) -> float:
    """Estimate a font size that fits OCR text within its source cell."""

    if source_cell and "".join(clean_ocr_text(text).split()) in {
        "学生基本情况",
        "家庭基本情况",
    }:
        source_widths = [
            float(width)
            for width in source_word_widths_pt
            if isfinite(float(width)) and float(width) > 0
        ]
        return max(
            6.0,
            min(14.0, median(source_widths) if source_widths else 9.0),
        )

    if source_cell:
        row_start = min(max(source_cell.row_start, 0), len(row_heights_pt))
        row_end = min(max(source_cell.row_end, 0), len(row_heights_pt))
        col_start = min(max(source_cell.col_start, 0), len(column_widths))
        col_end = min(max(source_cell.col_end, 0), len(column_widths))
    else:
        row_start = max(row_index, 0)
        row_end = min(row_index + 1, len(row_heights_pt))
        col_start = max(column_index, 0)
        col_end = min(column_index + 1, len(column_widths))

    width_pt = sum(column_widths[col_start:col_end]) / EMU_PER_PT
    height_pt = sum(row_heights_pt[row_start:row_end])
    if height_pt <= 0 and source_cell and source_cell.bottom > source_cell.top:
        height_pt = (source_cell.bottom - source_cell.top) * table_scale
    if width_pt <= 0 or height_pt <= 0:
        return 9.0

    normalized = "".join(clean_ocr_text(text).split())
    if not normalized:
        return 9.0
    minimum_size = 5.25
    source_heights = [
        float(height)
        for height in source_word_heights_pt
        if isfinite(float(height)) and float(height) > 0
    ]
    # Source OCR boxes are measured in image pixels. Once converted to points,
    # a fixed 9pt ceiling makes text in low-resolution scans much smaller than
    # it appears on the source page. Let the measured glyph height drive the
    # size, then keep the width and height fitting below as the safety bound.
    maximum_size = (
        min(14.0, median(source_heights) * 1.05)
        if source_heights
        else 9.0
    )
    maximum_size = max(minimum_size, maximum_size)
    available_width = max(width_pt - 4.5, 1.0)
    available_height = max(height_pt - 1.0, 1.0)

    def line_width(line: str) -> float:
        total = 0.0
        for character in line:
            if character.isspace():
                total += 0.3
            elif ord(character) >= 0x2E80:
                total += 1.0
            elif character in "ilI.,:;!'|":
                total += 0.35
            else:
                total += 0.55
        return total

    explicit_line_widths = [line_width(line) for line in text.splitlines() or [text]]
    font_size = maximum_size
    for _ in range(8):
        units_per_line = max(available_width / max(font_size, 1.0), 0.1)
        rendered_lines = sum(
            max(1, ceil(width / units_per_line))
            for width in explicit_line_widths
        )
        height_fit_size = available_height / max(rendered_lines * 1.12, 1.0)
        if height_fit_size >= font_size - 0.05:
            break
        font_size = max(minimum_size, min(font_size, height_fit_size))
    return max(minimum_size, min(maximum_size, font_size))


def _native_cell_required_height_pt(
    text: str,
    width_pt: float,
    font_size: float,
) -> float:
    """Estimate the minimum height needed to show every wrapped cell line."""

    if not text.strip() or width_pt <= 0 or font_size <= 0:
        return 0.0

    def line_width(line: str) -> float:
        total = 0.0
        for character in line:
            if character.isspace():
                total += 0.3
            elif ord(character) >= 0x2E80:
                total += 1.0
            elif character in "ilI.,:;!'|":
                total += 0.35
            else:
                total += 0.55
        return total

    available_width = max(width_pt - 4.5, 1.0)
    units_per_line = max(available_width / font_size, 0.1)
    rendered_lines = sum(
        max(1, ceil(line_width(line) / units_per_line))
        for line in text.splitlines() or [text]
    )
    return 1.0 + rendered_lines * font_size * 1.12


def _normalized_vertical_section_label(text: object) -> str:
    normalized_text = "".join(clean_ocr_text(text).split())
    for label in ("学生基本情况", "家庭基本情况"):
        if (
            normalized_text.startswith(label)
            and len(normalized_text) % len(label) == 0
            and normalized_text == label * (len(normalized_text) // len(label))
        ):
            return label
        if re.sub(r"(.)\1+", r"\1", normalized_text) == label:
            return label
    return normalized_text


def _native_cell_vertical_alignment(
    source_cell: TableCell | None,
    source_words: Iterable[PositionedWord],
) -> WD_CELL_VERTICAL_ALIGNMENT:
    """Match a native Word cell's vertical alignment to the scan geometry."""

    if source_cell is None:
        return WD_CELL_VERTICAL_ALIGNMENT.CENTER
    try:
        cell_top = float(source_cell.top)
        cell_bottom = float(source_cell.bottom)
    except (TypeError, ValueError, OverflowError):
        return WD_CELL_VERTICAL_ALIGNMENT.CENTER
    cell_height = cell_bottom - cell_top
    if not isfinite(cell_top) or not isfinite(cell_bottom) or cell_height <= 0:
        return WD_CELL_VERTICAL_ALIGNMENT.CENTER

    text_tops: list[float] = []
    text_bottoms: list[float] = []
    for word in source_words:
        if not clean_ocr_text(word.text):
            continue
        try:
            word_top = float(word.top)
            word_height = float(word.height)
        except (TypeError, ValueError, OverflowError):
            continue
        word_bottom = word_top + word_height
        if (
            not isfinite(word_top)
            or not isfinite(word_bottom)
            or word_height <= 0
            or word_bottom <= cell_top
            or word_top >= cell_bottom
        ):
            continue
        text_tops.append(max(word_top, cell_top))
        text_bottoms.append(min(word_bottom, cell_bottom))
    if not text_tops:
        return WD_CELL_VERTICAL_ALIGNMENT.CENTER

    content_top = min(text_tops)
    content_bottom = max(text_bottoms)
    content_height = content_bottom - content_top
    free_space = cell_height - content_height
    if free_space <= max(4.0, cell_height * 0.12):
        return WD_CELL_VERTICAL_ALIGNMENT.CENTER

    top_gap = max(content_top - cell_top, 0.0)
    bottom_gap = max(cell_bottom - content_bottom, 0.0)
    alignment_threshold = max(4.0, cell_height * 0.08)
    if top_gap <= alignment_threshold and bottom_gap - top_gap >= alignment_threshold:
        return WD_CELL_VERTICAL_ALIGNMENT.TOP
    if bottom_gap <= alignment_threshold and top_gap - bottom_gap >= alignment_threshold:
        return WD_CELL_VERTICAL_ALIGNMENT.BOTTOM
    return WD_CELL_VERTICAL_ALIGNMENT.CENTER


def _native_cell_horizontal_alignment(
    source_cell: TableCell | None,
    source_words: Iterable[PositionedWord],
    text: object,
) -> WD_ALIGN_PARAGRAPH | None:
    """Use short OCR text boxes to recover native cell alignment when clear."""

    normalized_text = clean_ocr_text(text)
    if source_cell is None or "□" in normalized_text or len(normalized_text.replace("\n", "")) > 24:
        return None
    try:
        cell_left = float(source_cell.left)
        cell_right = float(source_cell.right)
    except (TypeError, ValueError, OverflowError):
        return None
    cell_width = cell_right - cell_left
    if not isfinite(cell_left) or not isfinite(cell_right) or cell_width <= 0:
        return None

    word_lefts: list[float] = []
    word_rights: list[float] = []
    for word in source_words:
        if not clean_ocr_text(word.text):
            continue
        try:
            left = float(word.left)
            width = float(word.width)
        except (TypeError, ValueError, OverflowError):
            continue
        right = left + width
        center = left + width / 2.0
        if (
            not isfinite(left)
            or not isfinite(right)
            or width <= 0
            or not cell_left <= center <= cell_right
        ):
            continue
        word_lefts.append(max(left, cell_left))
        word_rights.append(min(right, cell_right))
    if not word_lefts:
        return None

    content_left = min(word_lefts)
    content_right = max(word_rights)
    free_space = cell_width - (content_right - content_left)
    if free_space <= max(4.0, cell_width * 0.12):
        return None

    left_gap = max(content_left - cell_left, 0.0)
    right_gap = max(cell_right - content_right, 0.0)
    if abs(left_gap - right_gap) <= max(8.0, cell_width * 0.15):
        return WD_ALIGN_PARAGRAPH.CENTER
    edge_tolerance = max(6.0, cell_width * 0.25)
    if left_gap < right_gap and left_gap <= edge_tolerance:
        return WD_ALIGN_PARAGRAPH.LEFT
    if right_gap < left_gap and right_gap <= edge_tolerance:
        return WD_ALIGN_PARAGRAPH.RIGHT
    return None


def _is_vertical_section_label(
    source_cell: TableCell | None,
    text: str,
    source_words: Iterable[PositionedWord] = (),
) -> bool:
    normalized_text = _normalized_vertical_section_label(text)
    if (
        source_cell is None
        or source_cell.right <= source_cell.left
        or source_cell.bottom - source_cell.top
        < (source_cell.right - source_cell.left) * 2.0
    ):
        return False
    if normalized_text in {"学生基本情况", "家庭基本情况"}:
        return True

    compact_text = "".join(clean_ocr_text(text).split())
    if not 3 <= len(compact_text) <= 20:
        return False
    stacked_lines = [
        "".join(clean_ocr_text(line).split())
        for line in str(text).splitlines()
        if clean_ocr_text(line)
    ]
    if len(stacked_lines) >= 3 and all(len(line) == 1 for line in stacked_lines):
        return True

    return any(
        "\n" in _native_word_text(word)
        and _normalized_vertical_section_label(word.text) == normalized_text
        for word in source_words
    )


def _set_vertical_section_label(
    cell,
    source_cell: TableCell | None,
    text: str,
    source_words: Iterable[PositionedWord] = (),
) -> None:
    """Keep tall section labels upright, with one glyph on each line."""
    if not _is_vertical_section_label(source_cell, text, source_words):
        return
    normalized_text = _normalized_vertical_section_label(text)

    # These labels are printed as upright, one-character lines. Some OCR table
    # results already contain those line breaks; adding Word's vertical text
    # direction on top rotates/reshapes the same content a second time.
    cell.text = "\n".join(normalized_text)
    cell_properties = cell._tc.get_or_add_tcPr()
    direction = cell_properties.find(qn("w:textDirection"))
    if direction is not None:
        cell_properties.remove(direction)


def _native_vertical_section_label_line_spacing_pt(
    source_cell: TableCell | None,
    text: str,
    source_words: Iterable[PositionedWord],
    table_scale: float,
) -> float | None:
    """Match vertical section-label line spacing to the OCR box geometry."""
    if not _is_vertical_section_label(source_cell, text, source_words):
        return None
    label = _normalized_vertical_section_label(text)
    matching_words = [
        word
        for word in source_words
        if _normalized_vertical_section_label(word.text) == label
        and isfinite(float(word.height))
        and float(word.height) > 0
    ]
    if not matching_words or not isfinite(table_scale) or table_scale <= 0:
        return None
    source_line_heights = [word.height / len(label) for word in matching_words]
    return max(6.0, min(24.0, median(source_line_heights) * table_scale))


def _apply_vertical_section_label_line_spacing(
    cell,
    line_spacing_pt: float | None,
) -> None:
    if line_spacing_pt is None:
        return
    for paragraph in cell.paragraphs:
        paragraph.paragraph_format.line_spacing = Pt(line_spacing_pt)


def _preserve_native_merged_text(existing_text: object, source_text: object) -> str:
    """Keep canonical OCR text and add only distinct text from merged grid cells."""

    existing = _deduplicate_repeated_native_lines(existing_text)
    source = _deduplicate_repeated_native_lines(source_text)
    if not existing:
        return source
    if not source:
        return existing

    source_compact = re.sub(
        r"家庭人均年收入_+",
        "家庭人均年收入",
        "".join(source.split()),
    )
    extras: list[str] = []
    seen_extras: set[str] = set()
    for line in existing.splitlines():
        normalized = clean_ocr_text(line)
        compact = re.sub(
            r"家庭人均年收入_+",
            "家庭人均年收入",
            "".join(normalized.split()),
        )
        if not compact:
            continue
        # A merged Word cell contains the text from every grid cell it absorbs.
        # Those cells often repeat wrapped fragments of the canonical OCR cell;
        # keep the complete source text once and discard those fragments.
        if compact in source_compact or source_compact in compact:
            continue
        if compact in seen_extras:
            continue
        seen_extras.add(compact)
        extras.append(normalized)

    return _deduplicate_repeated_native_lines("\n".join([source, *extras]))


def _deduplicate_repeated_native_lines(value: object) -> str:
    """Remove an exact repeated multi-line OCR block from one table cell.

    Table OCR providers sometimes emit a merged checklist twice, with another
    field such as the income line between the copies. Removing repeated blocks
    of at least two lines avoids dropping a legitimately repeated single label.
    """

    text = clean_ocr_text(value)
    lines = text.splitlines()
    if len(lines) < 4:
        return text

    def line_key(line: str) -> str:
        compact = "".join(clean_ocr_text(line).split())
        return re.sub(r"家庭人均年收入_+", "家庭人均年收入", compact)

    keys = [line_key(line) for line in lines]
    while len(lines) >= 4:
        duplicate_span: tuple[int, int] | None = None
        max_span = min(64, len(lines) // 2)
        for span in range(max_span, 1, -1):
            first_seen: dict[tuple[str, ...], int] = {}
            for start in range(len(keys) - span + 1):
                block = tuple(keys[start : start + span])
                if any(not line for line in block):
                    continue
                previous = first_seen.get(block)
                if previous is not None and start >= previous + span:
                    duplicate_span = (start, span)
                    break
                if previous is None:
                    first_seen[block] = start
            if duplicate_span is not None:
                break
        if duplicate_span is None:
            break
        start, span = duplicate_span
        del lines[start : start + span]
        del keys[start : start + span]

    return clean_ocr_text("\n".join(lines))


def _native_cell_text(
    result: OcrResult,
    source_cell: TableCell | None,
    fallback: object,
    positioned_words_for_cell: Iterable[PositionedWord] | None = None,
    cleaned_text_by_word_id: dict[int, str] | None = None,
) -> str:
    """Keep table OCR wording and use geometry only for known form structures."""

    fallback_text = _deduplicate_repeated_native_lines(fallback)
    if source_cell is None or source_cell.right <= source_cell.left or source_cell.bottom <= source_cell.top:
        return fallback_text
    words: list[PositionedWord] = []
    candidates = (
        result.positioned_words
        if positioned_words_for_cell is None
        else positioned_words_for_cell
    )
    for word in candidates:
        center_x = word.left + word.width / 2.0
        center_y = word.top + word.height / 2.0
        if not (
            source_cell.left <= center_x <= source_cell.right
            and source_cell.top <= center_y <= source_cell.bottom
        ):
            continue
        cleaned = (
            cleaned_text_by_word_id.get(id(word))
            if cleaned_text_by_word_id is not None
            else None
        )
        if cleaned is None:
            cleaned = clean_ocr_text(word.text)
        if cleaned:
            words.append(
                PositionedWord(
                    text=cleaned,
                    left=word.left,
                    top=word.top,
                    width=word.width,
                    height=word.height,
                    is_form_field=word.is_form_field,
                )
            )
    if not words:
        return fallback_text

    if fallback_text.count("□") < 2 and not any(
        marker in fallback_text
        for marker in ("省（自治区）", "县（市、区）", "镇（街道）", "村（居委）")
    ):
        return fallback_text

    return _deduplicate_repeated_native_lines(
        normalize_table_cell_value(fallback_text, words)
    )


def _native_word_text(word: PositionedWord, cleaned_text: str | None = None) -> str:
    """Keep narrow, tall OCR boxes as vertical labels in native tables."""

    text = clean_ocr_text(word.text) if cleaned_text is None else cleaned_text
    characters = list(text.replace("\n", ""))
    if (
        len(characters) >= 3
        and word.width > 0
        and word.height >= word.width * 3.0
        and word.height / len(characters) >= word.width * 0.75
    ):
        return "\n".join(characters)
    return text


def _set_table_width(table, width: int, indent: int = 0) -> None:
    table_properties = table._tbl.tblPr
    table_width = table_properties.find(qn("w:tblW"))
    if table_width is None:
        table_width = OxmlElement("w:tblW")
        table_properties.append(table_width)
    table_width.set(qn("w:type"), "dxa")
    table_width.set(qn("w:w"), str(int(width / 635)))
    layout = table_properties.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        table_properties.append(layout)
    layout.set(qn("w:type"), "fixed")
    table_indent = table_properties.find(qn("w:tblInd"))
    if table_indent is None:
        table_indent = OxmlElement("w:tblInd")
        table_properties.append(table_indent)
    table_indent.set(qn("w:w"), str(int(indent / 635)))
    table_indent.set(qn("w:type"), "dxa")
    for grid_column, column_width in zip(table._tbl.tblGrid, table.columns):
        grid_column.set(qn("w:w"), str(int(column_width.width / 635)))


def _set_cell_margins(table, top: int, start: int, bottom: int, end: int) -> None:
    for row in table.rows:
        for cell in row.cells:
            properties = cell._tc.get_or_add_tcPr()
            margins = properties.first_child_found_in("w:tcMar")
            if margins is None:
                margins = OxmlElement("w:tcMar")
                properties.append(margins)
            for side, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
                element = margins.find(qn(f"w:{side}"))
                if element is None:
                    element = OxmlElement(f"w:{side}")
                    margins.append(element)
                element.set(qn("w:w"), str(value))
                element.set(qn("w:type"), "dxa")


def _repeat_table_header(row) -> None:
    properties = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    properties.append(header)


def _create_image_pdf(
    image_path: Path,
    output_path: Path,
    *,
    title: str | None = None,
) -> None:
    """Create a legible scan PDF when LibreOffice is unavailable."""
    with Image.open(image_path) as image:
        source_width, source_height = image.size
        # Keep long receipt and mobile scans at a useful print width instead of
        # shrinking their entire contents to fit the short edge of an A4 page.
        # PDF readers support custom page heights up to 14,400 points; clamp
        # both axes with the same scale so table lines and text stay undistorted.
        max_page_width_pt = (
            A4_LONG_SIDE_PT if source_width > source_height else A4_SHORT_SIDE_PT
        )
        page_scale = min(
            max_page_width_pt / source_width,
            MAX_PDF_PAGE_HEIGHT_PT / source_height,
        )
        page_width_pt = source_width * page_scale
        page_height_pt = source_height * page_scale
        target_size = _print_export_image_size(
            source_width,
            source_height,
            page_width_pt,
            page_height_pt,
        )

        prepared, icc_profile = _image_to_srgb_on_white(image)
        try:
            if prepared.size != target_size:
                resized = prepared.resize(target_size, Image.Resampling.LANCZOS)
                prepared.close()
                prepared = resized
            dpi = (
                prepared.width * 72 / page_width_pt,
                prepared.height * 72 / page_height_pt,
            )
            save_options: dict[str, object] = {
                "format": "PDF",
                "dpi": dpi,
                "title": title or output_path.stem,
                # Pillow otherwise uses its default JPEG quality for PDF image
                # pages, which softens small scan text and thin form rules.
                "quality": 95,
                "subsampling": 0,
            }
            if isinstance(icc_profile, bytes):
                save_options["icc_profile"] = icc_profile
            prepared.save(output_path, **save_options)
        finally:
            prepared.close()


def _create_image_pdf_atomically(
    image_path: Path,
    output_path: Path,
    *,
    title: str | None = None,
) -> None:
    """Publish an image PDF only after the complete file has been written."""
    with tempfile.TemporaryDirectory(
        prefix="scan-image-pdf-",
        dir=output_path.parent,
    ) as temporary_dir:
        candidate = Path(temporary_dir) / output_path.name
        _create_image_pdf(image_path, candidate, title=title)
        if not _is_pdf_file(candidate):
            raise OSError("Image PDF conversion produced an invalid file")
        os.replace(candidate, output_path)


def _is_pdf_file(path: Path) -> bool:
    """Check the PDF header and trailer without reading the whole file."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return False
            tail_size = min(size, 64 * 1024)
            stream.seek(size - tail_size)
            tail = stream.read(tail_size)
        eof_index = tail.rfind(b"%%EOF")
        if eof_index < 0:
            return False
        startxref_index = tail.rfind(b"startxref", 0, eof_index)
        if startxref_index < 0:
            return False
        offset_text = tail[startxref_index + len(b"startxref"):eof_index].strip()
        if not re.fullmatch(rb"\d+", offset_text):
            return False
        xref_offset = int(offset_text)
        startxref_position = size - tail_size + startxref_index
        if xref_offset < 0 or xref_offset >= startxref_position:
            return False
        with path.open("rb") as stream:
            stream.seek(xref_offset)
            xref_marker = stream.read(32)
        return xref_marker.startswith(b"xref") or bool(
            re.match(rb"\d+\s+\d+\s+obj(?:\s|$)", xref_marker)
        )
    except OSError:
        return False


def _fallback_pdf_title(docx_path: Path) -> str:
    try:
        return Document(docx_path).core_properties.title or docx_path.stem
    except Exception:
        return docx_path.stem


def convert_docx_to_pdf(
    docx_path: Path,
    output_dir: Path,
    fallback_image_path: Path | None = None,
    prefer_image: bool = False,
) -> tuple[Path | None, bool]:
    """优先将可编辑 DOCX 转成文字型 PDF，失败时回退到图片型 PDF。

    返回值为 (PDF 路径, 是否使用图片回退)。
    """
    executable = os.getenv("LIBREOFFICE_BIN", "soffice")
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"{docx_path.stem}.pdf"
    if prefer_image and fallback_image_path and fallback_image_path.exists():
        _create_image_pdf_atomically(
            fallback_image_path,
            pdf_path,
            title=_fallback_pdf_title(docx_path),
        )
        return pdf_path, True
    executable_path = shutil.which(executable)
    if executable_path:
        # 使用独立 profile，避免服务并发请求撞上 LibreOffice 的用户锁。
        with tempfile.TemporaryDirectory(
            prefix="scan-convert-lo-",
            dir=output_dir,
        ) as workspace_dir:
            workspace = Path(workspace_dir)
            profile_dir = workspace / "profile"
            profile_dir.mkdir()
            converted_dir = workspace / "converted"
            converted_dir.mkdir()
            command = [
                executable_path,
                "--headless",
                f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                str(converted_dir.resolve()),
                str(docx_path),
            ]
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            candidate = converted_dir / pdf_path.name
            if (
                completed is not None
                and completed.returncode == 0
                and _is_pdf_file(candidate)
            ):
                os.replace(candidate, pdf_path)
                return pdf_path, False

    if fallback_image_path and fallback_image_path.exists():
        _create_image_pdf_atomically(
            fallback_image_path,
            pdf_path,
            title=_fallback_pdf_title(docx_path),
        )
        return pdf_path, True
    return None, False
