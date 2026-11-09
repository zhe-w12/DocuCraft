from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.ocr import OcrResult, ParagraphLayout


def _ocr_service() -> ModuleType:
    """Resolve the OCR facade lazily to keep legacy patch points working."""
    return importlib.import_module("app.services.ocr")


class GenericHttpOcrProvider:
    """Adapter for a self-hosted HTTP OCR gateway.

    The gateway may return plain text or positioned words (also accepted as
    ``words_result``). This adapter only normalizes the configured HTTP
    service response; it does not run a local OCR engine.
    """

    def __init__(self, endpoint: str, api_key: str | None) -> None:
        self.endpoint = endpoint
        self.api_key = api_key

    async def recognize(self, image_path: Path) -> "OcrResult":
        ocr = _ocr_service()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        image_mime_type = (
            "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        )
        async with ocr.httpx.AsyncClient(timeout=90) as client:
            with image_path.open("rb") as image_file:
                response = await client.post(
                    self.endpoint,
                    headers=headers,
                    files={"image": (image_path.name, image_file, image_mime_type)},
                )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("通用 OCR 返回格式必须是 JSON 对象")
        low_text_confidence = payload.get("low_text_confidence") is True
        low_confidence_examples = ocr._low_confidence_text_examples(payload)
        low_text_confidence = low_text_confidence or bool(low_confidence_examples)
        tables = ocr._bounded_generic_tables(payload.get("tables", []))
        has_tables = any(any(row for row in table) for table in tables)
        preserve_content = not has_tables
        try:
            with ocr.Image.open(image_path) as source:
                image_width, image_height = source.size
        except (OSError, ValueError):
            image_width, image_height = 0, 0
        recognized_word_texts: list[str] = []
        unpositioned_word_texts: list[str] = []
        positioned_words = ocr._generic_positioned_words(
            payload,
            preserve_content=preserve_content,
            image_width=image_width,
            image_height=image_height,
            recognized_texts=recognized_word_texts,
            unpositioned_texts=unpositioned_word_texts,
        )

        paragraphs = payload.get("paragraphs")
        if not isinstance(paragraphs, list):
            text = payload.get("text", "")
            paragraphs = [text] if text else []
        cleaned_paragraphs = []
        clean_text = ocr.clean_ocr_text if has_tables else ocr.clean_plain_ocr_text
        for item in paragraphs:
            cleaned = clean_text(item)
            if cleaned:
                cleaned_paragraphs.append(cleaned)
        if not has_tables and not cleaned_paragraphs and unpositioned_word_texts:
            recovered_text = ocr._join_ocr_lines(
                recognized_word_texts,
                preserve_content=True,
            )
            if recovered_text:
                # Invalid geometry cannot safely position these words. Keep
                # their original token order as editable prose.
                cleaned_paragraphs = [recovered_text]

        if not has_tables and unpositioned_word_texts:
            # Partial coordinates would hide words in coordinate-edit mode.
            positioned_words = []

        table_layouts = ocr._generic_table_layouts(
            payload.get("table_layouts"),
            image_width,
            image_height,
        )
        table_shapes = [
            (
                len(table),
                max(
                    (len(row) for row in table if isinstance(row, list)),
                    default=0,
                ),
            )
            if isinstance(table, list)
            else (0, 0)
            for table in tables
        ]
        table_cells = ocr._generic_table_cells(
            payload.get("table_cells"),
            image_width=image_width,
            image_height=image_height,
            table_shapes=table_shapes,
        )
        if not has_tables:
            # Invalid grids have no matching native table structure.
            tables = []
            table_layouts = []
            table_cells = []
        if has_tables and positioned_words and (not table_layouts or not table_cells):
            detected = ocr._table_from_image_lines(image_path, positioned_words)
            if detected is not None:
                if not table_layouts:
                    table_layouts = detected.table_layouts
                if not table_cells:
                    table_cells = detected.table_cells
        if positioned_words and table_cells:
            positioned_words = ocr._split_positioned_words_across_cells(
                [cell for table in table_cells for cell in table],
                positioned_words,
            )

        paragraph_styles: list[str] = []
        paragraph_layouts: list["ParagraphLayout | None"] = []
        if positioned_words:
            candidates = (
                ocr._paragraph_blocks_outside_tables(
                    [table_cells],
                    positioned_words,
                    image_width,
                )
                if has_tables
                and any(
                    cell.right > cell.left and cell.bottom > cell.top
                    for table in table_cells
                    for cell in table
                )
                else ocr._positioned_paragraph_candidates(
                    positioned_words,
                    image_width=image_width,
                    preserve_content=preserve_content,
                )
            )
            candidate_lengths, candidates_by_length, matcher_cache = (
                ocr._index_paragraph_candidates(candidates)
            )
            if not cleaned_paragraphs:
                cleaned_paragraphs = [item[0] for item in candidates]
            supplied_styles = payload.get("paragraph_styles")
            for index, paragraph in enumerate(cleaned_paragraphs):
                matched_index = ocr._matching_comparable_text_index(
                    paragraph,
                    candidate_lengths,
                    candidates_by_length,
                    matcher_cache,
                )
                matched = candidates[matched_index] if matched_index is not None else None
                if isinstance(supplied_styles, list) and index < len(supplied_styles):
                    style = str(supplied_styles[index])
                else:
                    style = matched[1] if matched is not None else "body"
                paragraph_styles.append(
                    style if style in {"title", "subtitle", "body"} else "body"
                )
                paragraph_layouts.append(matched[3] if matched is not None else None)
        else:
            paragraph_styles = ["body"] * len(cleaned_paragraphs)

        if positioned_words and not has_tables:
            detected = ocr._table_from_image_lines(image_path, positioned_words)
            if detected is not None:
                detected.low_text_confidence = low_text_confidence
                detected.low_confidence_examples = low_confidence_examples
                return detected
        return ocr.OcrResult(
            image_width=image_width,
            image_height=image_height,
            paragraphs=cleaned_paragraphs,
            paragraph_styles=paragraph_styles,
            paragraph_layouts=paragraph_layouts,
            positioned_words=ocr._as_positioned_word_objects(
                positioned_words,
                preserve_content=preserve_content,
            ),
            tables=tables,
            table_layouts=table_layouts,
            table_cells=table_cells,
            low_text_confidence=low_text_confidence,
            low_confidence_examples=low_confidence_examples,
        )
