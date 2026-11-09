from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TextFragment:
    """A positioned OCR fragment belonging to one line outside a table."""

    text: str
    left: float
    top: float
    width: float
    height: float


@dataclass
class ParagraphLayout:
    """Source-image coordinates for a paragraph or a form header line."""

    fragments: list[TextFragment] = field(default_factory=list)
    source_width: float = 0.0
    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0


@dataclass
class PositionedWord:
    """One OCR text block in source-image coordinates."""

    text: str
    left: float
    top: float
    width: float
    height: float
    is_form_field: bool = False


@dataclass
class TableCell:
    """A source cell, including merged-cell coordinates from table OCR."""

    row_start: int
    row_end: int
    col_start: int
    col_end: int
    text: str = ""
    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0


@dataclass
class TableLayout:
    """Table geometry reported by OCR, expressed in source-image pixels."""

    column_widths: list[float] = field(default_factory=list)
    row_heights: list[float] = field(default_factory=list)
    source_width: float = 0.0
    source_left: float = 0.0
    source_right: float = 0.0


@dataclass
class OcrResult:
    image_width: int = 0
    image_height: int = 0
    paragraphs: list[str] = field(default_factory=list)
    paragraph_styles: list[str] = field(default_factory=list)
    paragraph_layouts: list[ParagraphLayout | None] = field(default_factory=list)
    positioned_words: list[PositionedWord] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    table_layouts: list[TableLayout] = field(default_factory=list)
    table_cells: list[list[TableCell]] = field(default_factory=list)
    low_text_confidence: bool = False
    low_confidence_examples: list[str] = field(default_factory=list)
    used_general_text_fallback: bool = False
    orientation_correction_degrees: int = 0


class OcrLocalFallbackRequired(RuntimeError):
    """Signal that an image cannot fit a remote OCR request-size limit."""


class OcrNoTextRecognized(RuntimeError):
    """Signal that OCR completed but found no editable text in the image."""

    def __init__(
        self,
        message: str,
        *,
        image_width: int = 0,
        image_height: int = 0,
        orientation_correction_degrees: int = 0,
    ) -> None:
        super().__init__(message)
        self.image_width = image_width
        self.image_height = image_height
        self.orientation_correction_degrees = orientation_correction_degrees
