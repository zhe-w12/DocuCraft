from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import tempfile
import time
import uuid
import warnings
from dataclasses import asdict
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from PIL import Image, ImageOps
from dotenv import load_dotenv

from app.chat_protection import EndpointProtection, bounded_positive_int
from app.api_version import API_COMPATIBILITY_VERSION
from app.models import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantExportRequest,
    DocumentResponse,
    EditedDocumentRequest,
    PositionedWordRequest,
)
from app.request_body_limit import RequestBodySizeLimit
from app.source_fingerprint import backend_source_fingerprint
from app.services.assistant import (
    AssistantConfigurationError,
    AssistantProviderError,
    assistant_status,
    generate_reply,
    stream_reply,
)
from app.services.documents import (
    _coordinate_word_index,
    _coordinate_word_horizontal_bounds,
    _coordinate_word_vertical_bounds,
    _first_coordinate_word_match,
    _fit_coordinate_word_edits,
    _has_positioned_word_changes,
    _merge_coordinate_words,
    convert_docx_to_pdf,
    create_docx,
    create_assistant_docx,
    create_hybrid_editable_docx,
    create_native_table_docx,
    render_positioned_image,
)
from app.services.image_color import _image_to_srgb_on_white, _thumbnail_ocr_input
from app.services.editor import (
    _join_cell_words,
    _positioned_words_in_cell,
    recover_source_table_cells,
    render_editor_html,
    sample_source_paper_colors,
)
from app.services.ocr import (
    GenericHttpOcrProvider,
    MockOcrProvider,
    OcrLocalFallbackRequired,
    OcrNoTextRecognized,
    OcrResult,
    ParagraphLayout,
    PositionedWord,
    TableCell,
    TableLayout,
    TextFragment,
    clean_ocr_text,
    get_ocr_provider,
    normalize_table_cell_text,
    reflow_plain_text_result,
    recognize_locally,
    _rotate_ocr_image_copy,
)
from app.services.storage import DocumentStorage, is_non_empty_file


load_dotenv(Path(__file__).resolve().parents[2] / ".env")
logger = logging.getLogger(__name__)


BACKEND_SOURCE_FINGERPRINT = backend_source_fingerprint()


def _stored_document_warnings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        warning = item.strip()
        if warning and warning not in result:
            result.append(warning)
    return result


def _stored_low_confidence_examples(
    value: object,
    warnings: object = None,
) -> list[str]:
    examples = value if isinstance(value, list) else []
    result: list[str] = []

    def append_examples(items: list[object]) -> None:
        for item in items:
            if not isinstance(item, str):
                continue
            example = item.strip()[:64]
            if example and example not in result:
                result.append(example)
            if len(result) >= 5:
                return

    append_examples(examples)
    if result or not isinstance(warnings, list):
        return result

    # Earlier versions stored the examples only in the user-facing warning.
    # Recover those snippets so existing history keeps its review markers.
    marker = "建议优先核对："
    for warning in warnings:
        if not isinstance(warning, str) or marker not in warning:
            continue
        serialized_examples = warning.split(marker, 1)[1].split("；", 1)[0]
        append_examples(serialized_examples.split("、"))
        break
    return result


TABLE_IMAGE_PDF_WARNING = "已生成原图版式 PDF"
TABLE_TEXT_PDF_WARNING = "已生成可复制文字 PDF"
TABLE_VISUAL_PDF_WARNING = "可编辑表格 Word 暂不可用，PDF 按原图版式导出；请核对识别文字"
TABLE_PDF_UNAVAILABLE_WARNING = "PDF 暂未生成，请下载 Word"
TABLE_TEXT_PDF_UNAVAILABLE_WARNING = "可复制文字 PDF 暂未生成"
TABLE_STRUCTURED_DOCX_WARNING = "已生成可编辑 Word 表格；原图版 Word 可用于逐项对照"
TABLE_STRUCTURED_DOCX_UNAVAILABLE_WARNING = "可编辑 Word 表格生成失败，已保留完整原图版 Word"
TEXT_STRUCTURED_DOCX_WARNING = "已生成可编辑文字版 Word；原图版 Word 保留扫描版式"
TEXT_STRUCTURED_DOCX_UNAVAILABLE_WARNING = "可编辑文字版 Word 暂不可用，原图版 Word 仍可下载"


def _serialize_positioned_word(word: PositionedWord) -> dict[str, object]:
    """Keep the original wire shape unless a word is explicitly a form field."""
    serialized = asdict(word)
    if not word.is_form_field:
        serialized.pop("is_form_field", None)
    return serialized
TABLE_PDF_WARNINGS = {
    TABLE_IMAGE_PDF_WARNING,
    TABLE_TEXT_PDF_WARNING,
    TABLE_VISUAL_PDF_WARNING,
    TABLE_PDF_UNAVAILABLE_WARNING,
    TABLE_TEXT_PDF_UNAVAILABLE_WARNING,
}
TABLE_STRUCTURED_DOCX_WARNINGS = {
    TABLE_STRUCTURED_DOCX_WARNING,
    TABLE_STRUCTURED_DOCX_UNAVAILABLE_WARNING,
    TEXT_STRUCTURED_DOCX_WARNING,
    TEXT_STRUCTURED_DOCX_UNAVAILABLE_WARNING,
}


def _set_table_pdf_warning(warnings: list[str], *messages: str) -> None:
    """Keep the status for the current table PDF format without stale notices."""
    warnings[:] = [warning for warning in warnings if warning not in TABLE_PDF_WARNINGS]
    for message in messages:
        if message and message not in warnings:
            warnings.append(message)


def _is_image_close_error(error: BaseException) -> bool:
    messages = (str(error), str(getattr(error, "detail", "")))
    response = getattr(error, "response", None)
    if response is not None:
        # httpx.HTTPStatusError's string only contains the status and URL.
        # OCR gateways often put the useful JavaScript failure in the response
        # body, so inspect a small prefix before deciding whether to retry or
        # fall back to local OCR.
        try:
            content = response.content
            if isinstance(content, bytes):
                messages += (content[:8192].decode("utf-8", errors="replace"),)
            elif isinstance(content, str):
                messages += (content[:8192],)
        except Exception:
            pass
    return bool(
        any(
            re.search(
                r"Cannot read properties of null\s*\(reading ['\"]close['\"]\)",
                message,
                re.IGNORECASE,
            )
            for message in messages
        )
    )


def _is_transient_ocr_error(error: BaseException) -> bool:
    if _is_image_close_error(error) or isinstance(error, httpx.TransportError):
        return True
    response = getattr(error, "response", None)
    try:
        status_code = int(response.status_code)
    except (AttributeError, TypeError, ValueError):
        return False
    return status_code in {408, 425, 429, 500, 502, 503, 504}


def _retry_after_seconds(error: BaseException) -> int | None:
    """Normalize an upstream Retry-After value for the browser retry handler."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    raw_value = headers.get("Retry-After") if headers is not None else None
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None

    try:
        delay = float(raw_value.strip())
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = retry_at.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None

    if not math.isfinite(delay) or delay <= 0:
        return None
    return max(1, math.ceil(delay))


def _ocr_result_has_content(result: OcrResult) -> bool:
    def has_text(value: object) -> bool:
        return isinstance(value, str) and bool(clean_ocr_text(value))

    if any(has_text(paragraph) for paragraph in result.paragraphs):
        return True
    if any(has_text(word.text) for word in result.positioned_words):
        return True
    if any(
        has_text(cell)
        for table in result.tables
        for row in table
        for cell in row
    ):
        return True
    if any(has_text(cell.text) for cells in result.table_cells for cell in cells):
        return True
    return any(
        has_text(fragment.text)
        for layout in result.paragraph_layouts
        if layout is not None
        for fragment in layout.fragments
    )


def _ocr_result_character_count(result: OcrResult) -> int:
    """Count table text and any text positioned outside the table grid."""
    if result.tables:
        values = [value for table in result.tables for row in table for value in row]
        values.extend(result.paragraphs)
    elif result.paragraphs:
        values = result.paragraphs
    elif result.table_cells:
        values = [cell.text for cells in result.table_cells for cell in cells]
    else:
        values = [word.text for word in result.positioned_words]
    return sum(len("".join(clean_ocr_text(value).split())) for value in values)


def _ocr_result_paragraph_character_count(result: OcrResult) -> int:
    return sum(
        len("".join(clean_ocr_text(paragraph).split()))
        for paragraph in result.paragraphs
    )


def _ocr_result_table_slot_count(result: OcrResult) -> int:
    return sum(
        max(len(row), 1)
        for table in result.tables
        for row in table
    )


def _ocr_result_table_dimensions(result: OcrResult) -> list[tuple[int, int]]:
    return [
        (len(table), max((len(row) for row in table), default=0))
        for table in result.tables
    ]


def _prefer_local_ocr_result(cloud_result: OcrResult, local_result: OcrResult) -> bool:
    """Use a high-confidence local retry only when it retains cloud coverage."""
    if local_result.low_text_confidence or not _ocr_result_has_content(local_result):
        return False
    if cloud_result.tables and not local_result.tables:
        return False

    cloud_characters = _ocr_result_character_count(cloud_result)
    local_characters = _ocr_result_character_count(local_result)
    if local_characters < max(1, math.ceil(cloud_characters * 0.75)):
        return False

    cloud_paragraph_characters = _ocr_result_paragraph_character_count(cloud_result)
    if cloud_paragraph_characters:
        local_paragraph_characters = _ocr_result_paragraph_character_count(local_result)
        if local_paragraph_characters < max(
            1,
            math.ceil(cloud_paragraph_characters * 0.75),
        ):
            return False

    if cloud_result.tables:
        if len(local_result.tables) < len(cloud_result.tables):
            return False
        cloud_dimensions = _ocr_result_table_dimensions(cloud_result)
        local_dimensions = _ocr_result_table_dimensions(local_result)
        for (cloud_rows, cloud_columns), (local_rows, local_columns) in zip(
            cloud_dimensions,
            local_dimensions,
        ):
            if (
                local_rows < math.ceil(cloud_rows * 0.8)
                or local_columns < math.ceil(cloud_columns * 0.8)
            ):
                return False
        cloud_slots = _ocr_result_table_slot_count(cloud_result)
        local_slots = _ocr_result_table_slot_count(local_result)
        if local_slots < math.ceil(cloud_slots * 0.8):
            return False
    return True


async def _recognize_with_transient_ocr_retry(
    provider,
    image_path: Path,
    *,
    text_source_path: Path | None = None,
) -> tuple[OcrResult, bool]:
    orientation_correction_degrees = 0
    retry_provider = True
    low_confidence_result: OcrResult | None = None

    async def recognize_once() -> OcrResult:
        source_recognizer = getattr(provider, "recognize_with_text_source", None)
        if text_source_path is not None and callable(source_recognizer):
            return await source_recognizer(image_path, text_source_path)
        return await provider.recognize(image_path)

    try:
        result = await recognize_once()
        orientation_correction_degrees = result.orientation_correction_degrees
        if _ocr_result_has_content(result):
            if not result.low_text_confidence:
                return result, False
            low_confidence_result = result
            retry_provider = False
            last_error = RuntimeError("OCR provider returned low-confidence text")
        else:
            retry_provider = False
            last_error = RuntimeError("OCR provider returned no recognized text")
    except Exception as exc:
        # A timed-out OCR request has already consumed most of the browser's
        # request budget. Retry quick transient failures, but go to local OCR
        # immediately when the upstream itself exceeded its timeout.
        retry_provider = not isinstance(exc, httpx.TimeoutException)
        detected_rotation = getattr(
            exc,
            "_iscan_ocr_orientation_correction_degrees",
            0,
        )
        if detected_rotation in {-90, 90, 180}:
            orientation_correction_degrees = detected_rotation
        if not _is_transient_ocr_error(exc) and not isinstance(
            exc,
            OcrLocalFallbackRequired,
        ) and not orientation_correction_degrees:
            raise
        last_error = exc

    response = getattr(last_error, "response", None)
    try:
        status_code = int(response.status_code)
    except (AttributeError, TypeError, ValueError):
        status_code = 0
    if (
        retry_provider
        and status_code != 429
        and not isinstance(last_error, OcrLocalFallbackRequired)
    ):
        logger.warning(
            "OCR provider returned no usable text or temporarily failed; "
            "retrying once before reporting the service error "
            "(status_code=%s, error_type=%s)",
            status_code or "transport",
            type(last_error).__name__,
        )
        await asyncio.sleep(1)
        try:
            result = await recognize_once()
            orientation_correction_degrees = (
                result.orientation_correction_degrees
                or orientation_correction_degrees
            )
            if _ocr_result_has_content(result):
                if result.low_text_confidence:
                    low_confidence_result = result
                    last_error = RuntimeError(
                        "OCR provider returned low-confidence text after retry"
                    )
                elif (
                    not orientation_correction_degrees
                    or result.orientation_correction_degrees
                ):
                    return result, False
                else:
                    last_error = RuntimeError(
                        "OCR provider returned no recognized text after retry"
                    )
            else:
                last_error = RuntimeError("OCR provider returned no recognized text after retry")
        except Exception as retry_exc:
            detected_rotation = getattr(
                retry_exc,
                "_iscan_ocr_orientation_correction_degrees",
                0,
            )
            if detected_rotation in {-90, 90, 180}:
                orientation_correction_degrees = detected_rotation
            last_error = retry_exc

    if isinstance(provider, (GenericHttpOcrProvider, MockOcrProvider)):
        if low_confidence_result is not None:
            return low_confidence_result, False
        raise last_error

    try:
        with tempfile.TemporaryDirectory(prefix="iscan-local-ocr-") as temp_dir:
            local_image_path = Path(temp_dir) / "local-ocr.png"
            local_source_path = (
                text_source_path
                if text_source_path is not None and text_source_path.is_file()
                else image_path
            )
            await asyncio.to_thread(
                _prepare_local_ocr_image,
                local_source_path,
                local_image_path,
                rotation_degrees=orientation_correction_degrees,
            )
            result = await asyncio.to_thread(recognize_locally, local_image_path)
        total_rotation = (
            orientation_correction_degrees + result.orientation_correction_degrees
        ) % 360
        result.orientation_correction_degrees = {
            0: 0,
            90: 90,
            180: 180,
            270: -90,
        }[total_rotation]
        if not _ocr_result_has_content(result):
            raise RuntimeError("Local OCR returned no recognized text")
    except OcrNoTextRecognized as fallback_exc:
        if low_confidence_result is not None:
            return low_confidence_result, False
        if _is_transient_ocr_error(last_error):
            # Give the client a stable retry signal when both the OCR service
            # request and local recognition fail. Returning an empty success
            # here prevents the browser from retrying a known transient error.
            raise last_error
        total_rotation = (
            orientation_correction_degrees
            + fallback_exc.orientation_correction_degrees
        ) % 360
        orientation_degrees = {
            0: 0,
            90: 90,
            180: 180,
            270: -90,
        }[total_rotation]
        dimensions_path = (
            text_source_path
            if text_source_path is not None and text_source_path.is_file()
            else image_path
        )
        try:
            with Image.open(dimensions_path) as source_image:
                image_width, image_height = source_image.size
        except (OSError, ValueError):
            image_width = fallback_exc.image_width
            image_height = fallback_exc.image_height
        if total_rotation in {90, 270}:
            image_width, image_height = image_height, image_width
        logger.warning(
            "Cloud and local OCR returned no editable text; preserving the source image "
            "(error_type=%s)",
            type(fallback_exc).__name__,
        )
        return OcrResult(
            image_width=image_width,
            image_height=image_height,
            orientation_correction_degrees=orientation_degrees,
        ), False
    except Exception as fallback_exc:
        if low_confidence_result is not None:
            logger.warning(
                "Local OCR candidate was unavailable for low-confidence cloud text "
                "(error_type=%s)",
                type(fallback_exc).__name__,
            )
            return low_confidence_result, False
        logger.warning(
            "Local OCR fallback was unavailable after OCR provider failure "
            "(error_type=%s)",
            type(fallback_exc).__name__,
        )
        raise last_error
    if low_confidence_result is not None:
        if _prefer_local_ocr_result(low_confidence_result, result):
            logger.warning(
                "Local OCR replaced a low-confidence cloud result after coverage checks"
            )
            return result, True
        return low_confidence_result, False
    logger.warning(
        "OCR provider returned no usable text or remained unavailable; "
        "completed import with local OCR"
    )
    return result, True


def max_upload_bytes() -> int:
    return bounded_positive_int("MAX_UPLOAD_MB", 20, 200) * 1024 * 1024


def max_source_image_bytes() -> int:
    configured_limit = bounded_positive_int("MAX_SOURCE_IMAGE_MB", 100, 200) * 1024 * 1024
    return max(max_upload_bytes(), configured_limit)


app = FastAPI(title="DocuCraft API", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def request_validation_error_response(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return safe validation details without echoing invalid input values."""

    details = []
    for error in exc.errors():
        location = [
            part if isinstance(part, (str, int)) else str(part)
            for part in error.get("loc", ())
        ]
        details.append({
            "loc": location,
            "msg": str(error.get("msg", "请求内容无效")),
            "type": str(error.get("type", "value_error")),
        })
    return JSONResponse(status_code=422, content={"detail": details})


# Bump when structured OCR Word output changes. The refresh route uses the
# version to rebuild cached editable text and table exports from older layouts.
STRUCTURED_DOCX_LAYOUT_VERSION = "52"
# Bump when copyable OCR PDF generation changes.
TABLE_PDF_LAYOUT_VERSION = "37"
# Bump when the scan-faithful image PDF generation changes.
VISUAL_PDF_LAYOUT_VERSION = "2"
PLAIN_TEXT_LAYOUT_VERSION = "2"
# Bump this whenever the scan-faithful Word output changes. The refresh route
# uses it to rebuild stale image-layout documents from older implementations.
VISUAL_DOCX_LAYOUT_VERSION = "22"
# Added before the route guards in source order so rate limits run before the
# bounded body is read; CORS is registered last to wrap any 413 response.
app.add_middleware(
    RequestBodySizeLimit,
    path="/api/v1/documents/convert",
    max_bytes=max_source_image_bytes() + 1024 * 1024,
    detail="图片原文件超过服务器允许大小，请缩小后重试",
)
app.add_middleware(
    RequestBodySizeLimit,
    paths=(
        "/api/v1/assistant/chat",
        "/api/v1/assistant/export/docx",
        "/api/v1/assistant/export/pdf",
        "/api/v1/documents/export-edited",
    ),
    max_bytes=8 * 1024 * 1024,
    detail="请求内容过大，请缩小后重试",
)
# Register CORS last so browser clients can read the guards' 429/503 responses.
app.add_middleware(
    EndpointProtection,
    paths=("/api/v1/documents/convert",),
    method="POST",
    requests_per_minute=bounded_positive_int("OCR_REQUESTS_PER_MINUTE", 6, 1000),
    max_concurrent=bounded_positive_int("OCR_MAX_CONCURRENT", 2, 100),
    max_concurrent_per_client=bounded_positive_int("OCR_MAX_CONCURRENT_PER_CLIENT", 1, 100),
    rate_limit_detail="图片识别请求过于频繁，请稍后再试",
    busy_detail="图片识别服务当前较忙，请稍后重试",
)
app.add_middleware(
    EndpointProtection,
    paths=(
        "/api/v1/documents/export-edited",
        "/api/v1/assistant/export/docx",
        "/api/v1/assistant/export/pdf",
    ),
    method="POST",
    requests_per_minute=bounded_positive_int("DOCUMENT_EXPORT_REQUESTS_PER_MINUTE", 10, 1000),
    max_concurrent=bounded_positive_int("DOCUMENT_EXPORT_MAX_CONCURRENT", 3, 100),
    max_concurrent_per_client=bounded_positive_int("DOCUMENT_EXPORT_MAX_CONCURRENT_PER_CLIENT", 1, 100),
    rate_limit_detail="文档导出请求过于频繁，请稍后再试",
    busy_detail="文档导出服务当前较忙，请稍后重试",
)
app.add_middleware(
    EndpointProtection,
    path_prefix="/api/v1/documents/",
    path_suffix="/refresh",
    method="GET",
    requests_per_minute=bounded_positive_int("DOCUMENT_REFRESH_REQUESTS_PER_MINUTE", 30, 1000),
    max_concurrent=bounded_positive_int("DOCUMENT_REFRESH_MAX_CONCURRENT", 4, 100),
    max_concurrent_per_client=bounded_positive_int("DOCUMENT_REFRESH_MAX_CONCURRENT_PER_CLIENT", 2, 100),
    rate_limit_detail="文档刷新请求过于频繁，请稍后再试",
    busy_detail="文档刷新服务当前较忙，请稍后重试",
)
app.add_middleware(
    EndpointProtection,
    paths=("/api/v1/assistant/chat",),
    method="POST",
    requests_per_minute=bounded_positive_int("AI_CHAT_REQUESTS_PER_MINUTE", 10, 1000),
    max_concurrent=bounded_positive_int("AI_CHAT_MAX_CONCURRENT", 4, 100),
    max_concurrent_per_client=bounded_positive_int("AI_CHAT_MAX_CONCURRENT_PER_CLIENT", 2, 100),
    rate_limit_detail="AI 请求过于频繁，请稍后再试",
    busy_detail="AI 助手当前较忙，请稍后重试",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After", "X-Conversion-ID", "X-Error-Code"],
)

storage = DocumentStorage()
app.mount("/files", StaticFiles(directory=storage.root), name="files")


def metadata_path(document_id: str) -> Path:
    """Keep OCR layout metadata outside the publicly mounted files directory."""
    if (
        not isinstance(document_id, str)
        or len(document_id) != 32
        or any(character not in "0123456789abcdef" for character in document_id)
    ):
        raise HTTPException(status_code=404, detail="文档不存在")
    directory = storage.root.parent / f"{storage.root.name}_metadata"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{document_id}.json"


def source_image_endpoint(
    request: Request,
    document_id: str,
    *,
    original: bool = False,
) -> str:
    url = f"{str(request.base_url).rstrip('/')}/api/v1/documents/{document_id}/source-image"
    return f"{url}?original=true" if original else url


def document_download_endpoint(document_id: str, file_kind: str) -> str:
    cache_buster = uuid.uuid4().hex
    return f"/api/v1/documents/{document_id}/download/{file_kind}?v={cache_buster}"


def _safe_document_stem(value: object) -> str:
    """Make a portable, readable filename stem from an uploaded or OCR title."""
    raw_name = str(value or "").strip()
    base_name = re.split(r"[\\/]", raw_name)[-1]
    stem = Path(base_name).stem
    stem = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    stem = "".join(list(stem)[:80]).rstrip(" .")
    if not stem:
        return "扫描文档"
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", stem):
        return f"_{stem}"
    return stem


def _metadata_document_title(
    metadata: dict[str, object],
    fallback: object = "扫描文档",
) -> str:
    stored_title = metadata.get("title")
    if isinstance(stored_title, str) and stored_title.strip():
        return _safe_document_stem(stored_title)

    paragraphs = metadata.get("paragraphs")
    styles = metadata.get("paragraph_styles")
    if isinstance(paragraphs, list):
        if isinstance(styles, list):
            for index, paragraph in enumerate(paragraphs):
                if (
                    index < len(styles)
                    and styles[index] == "title"
                    and isinstance(paragraph, str)
                    and paragraph.strip()
                ):
                    return _safe_document_stem(paragraph)
        for paragraph in paragraphs:
            if isinstance(paragraph, str) and paragraph.strip():
                return _safe_document_stem(paragraph)

    tables = metadata.get("tables")
    if isinstance(tables, list):
        for table in tables:
            if not isinstance(table, list):
                continue
            for row in table:
                if not isinstance(row, list):
                    continue
                for cell in row:
                    if isinstance(cell, str) and cell.strip():
                        return _safe_document_stem(cell)

    fallback_text = str(fallback or "")
    if re.fullmatch(r"(?i:[a-f0-9]{24,64})", fallback_text.strip()):
        return "扫描文档"
    return _safe_document_stem(fallback_text)


def _document_download_filename(
    document_id: str,
    file_kind: str,
    metadata: dict[str, object],
) -> str:
    title = _metadata_document_title(metadata, document_id)
    has_tables = isinstance(metadata.get("tables"), list) and bool(metadata["tables"])
    if file_kind == "structured-docx":
        return f"{title}_{'可编辑表格' if has_tables else '可编辑文字'}.docx"
    if file_kind == "structured-pdf":
        return f"{title}_可复制文字.pdf"
    if file_kind == "docx" and (
        metadata.get("visual_docx_layout_version") == VISUAL_DOCX_LAYOUT_VERSION
        or has_tables
    ):
        return f"{title}_原图版式.docx"
    if file_kind == "pdf":
        if (
            metadata.get("visual_pdf_layout_version") == VISUAL_PDF_LAYOUT_VERSION
            or has_tables
        ):
            return f"{title}_原图版式.pdf"
        return f"{title}.pdf"
    return f"{title}.docx"


def _source_image_suffix(name: object) -> str:
    """Use a safe temporary suffix that matches stored source image files."""
    suffix = Path(name).suffix.lower() if isinstance(name, str) else ""
    return suffix if suffix in {".jpg", ".jpeg", ".png"} else ".jpg"


def _stored_image_dimensions(document_id: str, metadata: dict[str, object]) -> tuple[int, int]:
    """Read dimensions for old metadata that predates image_width/image_height."""

    try:
        width = int(metadata.get("image_width", 0) or 0)
        height = int(metadata.get("image_height", 0) or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width > 0 and height > 0:
        return width, height

    name = metadata.get("source_image_file")
    if not isinstance(name, str) or not name:
        return 0, 0
    candidate = storage.path(Path(name).name)
    temporary: Path | None = None
    try:
        if not candidate.exists() and storage._remote:
            temporary = storage.path(
                f"{document_id}_dimension_probe{_source_image_suffix(name)}"
            )
            storage.download_published(name, temporary)
            candidate = temporary
        if candidate.exists():
            with Image.open(candidate) as image:
                width, height = image.size
                if image.getexif().get(274, 1) in {5, 6, 7, 8}:
                    return height, width
                return width, height
    except (OSError, ValueError, RuntimeError):
        return 0, 0
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return 0, 0


async def save_upload(upload: UploadFile, destination: Path) -> None:
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > max_source_image_bytes():
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail="图片原文件超过服务器允许大小，请缩小后重试",
                )
            output.write(chunk)


def _prepare_local_ocr_image(
    source: Path,
    destination: Path,
    *,
    rotation_degrees: int = 0,
) -> tuple[int, int]:
    """Prepare a high-resolution, bounded-size image for local OCR fallback."""
    transpose = {
        -90: Image.Transpose.ROTATE_270,
        90: Image.Transpose.ROTATE_90,
        180: Image.Transpose.ROTATE_180,
    }.get(rotation_degrees)
    if rotation_degrees and transpose is None:
        raise ValueError("OCR orientation correction must be a right angle")

    with Image.open(source) as image:
        if image.format in {"JPEG", "MPO"} and image.mode in {"L", "RGB"}:
            image.draft(image.mode, (4096, 4096))
        ImageOps.exif_transpose(image, in_place=True)
        oriented = image
        try:
            rgb, icc_profile = _image_to_srgb_on_white(
                oriented,
                force_srgb=True,
            )
            try:
                if transpose is not None:
                    rotated = rgb.transpose(transpose)
                    rgb.close()
                    rgb = rotated
                rgb.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
                longest_edge = max(rgb.size)
                if longest_edge < 2400:
                    scale = min(2.0, 2400 / longest_edge)
                    enlarged = rgb.resize(
                        (
                            max(1, round(rgb.width * scale)),
                            max(1, round(rgb.height * scale)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                    rgb.close()
                    rgb = enlarged
                width, height = rgb.size
                # Small phone screenshots often have tiny glyphs despite
                # reporting 300 DPI. Upsample only this OCR fallback copy;
                # callers map these dimensions back to the stored source.
                # Keep pixels lossless because JPEG artifacts can erase thin
                # strokes from small or low-contrast text.
                save_options: dict[str, object] = {
                    "format": "PNG",
                    "compress_level": 1,
                    "dpi": (300, 300),
                }
                if isinstance(icc_profile, bytes):
                    save_options["icc_profile"] = icc_profile
                rgb.save(destination, **save_options)
                return width, height
            finally:
                rgb.close()
        finally:
            if oriented is not image:
                oriented.close()


def _normalized_ocr_suffix(source: Path) -> str:
    """Keep lossless uploads lossless in the bounded OCR working copy."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                source_format = (image.format or "").upper()
    except (Image.DecompressionBombError, OSError, ValueError):
        # Let normalize_image return its existing, user-safe invalid-image error.
        return ".jpg"
    return ".jpg" if source_format in {"JPEG", "MPO"} else ".png"


def normalize_image(source: Path, destination: Path) -> None:
    try:
        # Pillow's decompression-bomb warning threshold is higher than this
        # service's limit. Suppress its warning so rejected headers do not
        # flood logs; the explicit check below returns a stable API error.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            opened_image = Image.open(source)

        with opened_image as image:
            width, height = image.size
            max_pixels = bounded_positive_int("MAX_IMAGE_PIXELS", 50_000_000, 100_000_000)
            if width * height > max_pixels:
                raise HTTPException(status_code=413, detail="图片分辨率过高，请缩小尺寸后重试")
            if image.format in {"JPEG", "MPO"} and image.mode in {"L", "RGB"}:
                # JPEG supports decoding a reduced-resolution draft, which
                # avoids materializing full-size phone photos before thumbnailing.
                # Keep the source channel mode so its ICC profile remains valid.
                image.draft(image.mode, (4000, 4000))
            ImageOps.exif_transpose(image, in_place=True)
            oriented = image
            try:
                thumbnail_image, close_thumbnail_image = _thumbnail_ocr_input(
                    oriented,
                    (2500, 2500),
                )
                try:
                    rgb_image, icc_profile = _image_to_srgb_on_white(
                        thumbnail_image,
                        force_srgb=True,
                    )
                finally:
                    if close_thumbnail_image:
                        thumbnail_image.close()
                try:
                    if destination.suffix.lower() == ".png":
                        # Preserve fine text and checkbox strokes from PNG and
                        # other non-JPEG sources. The OCR provider compresses
                        # this bounded copy only when its request limit requires it.
                        save_options: dict[str, object] = {
                            "format": "PNG",
                            "compress_level": 1,
                        }
                    else:
                        save_options = {
                            "format": "JPEG",
                            "quality": 92,
                            "optimize": True,
                        }
                    if isinstance(icc_profile, bytes):
                        save_options["icc_profile"] = icc_profile
                    rgb_image.save(destination, **save_options)
                finally:
                    rgb_image.close()
            finally:
                if oriented is not image:
                    oriented.close()
    except HTTPException:
        raise
    except Image.DecompressionBombError as exc:
        raise HTTPException(status_code=413, detail="图片分辨率过高，请缩小尺寸后重试") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无法读取图片，请检查文件后重试") from exc


def preserve_source_image(source: Path, document_id: str) -> tuple[Path, int, int]:
    """Keep a full-resolution editor background, preserving common source bytes."""
    with Image.open(source) as image:
        image_format = (image.format or "").upper()
        orientation = image.getexif().get(274, 1)
        source_width, source_height = image.size
        if orientation in {5, 6, 7, 8}:
            source_width, source_height = source_height, source_width

        suffix = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png"}.get(image_format)
        preserve_bytes = suffix is not None and orientation == 1
        if not preserve_bytes:
            suffix = ".png"
        destination = storage.path(f"{document_id}_source{suffix}")

        if preserve_bytes:
            shutil.copyfile(source, destination)
        else:
            ImageOps.exif_transpose(image, in_place=True)
            oriented = image
            try:
                source_width, source_height = oriented.size
                rgb_image, icc_profile = _image_to_srgb_on_white(oriented)
                try:
                    save_options: dict[str, object] = {
                        "format": "PNG",
                        "optimize": True,
                    }
                    if isinstance(icc_profile, bytes):
                        save_options["icc_profile"] = icc_profile

                    dpi = oriented.info.get("dpi")
                    if (
                        isinstance(dpi, (tuple, list))
                        and len(dpi) == 2
                        and all(
                            isinstance(value, (int, float))
                            and math.isfinite(value)
                            and value > 0
                            for value in dpi
                        )
                    ):
                        normalized_dpi = (float(dpi[0]), float(dpi[1]))
                        if orientation in {5, 6, 7, 8}:
                            normalized_dpi = (normalized_dpi[1], normalized_dpi[0])
                        save_options["dpi"] = normalized_dpi

                    rgb_image.save(destination, **save_options)
                finally:
                    rgb_image.close()
            finally:
                if oriented is not image:
                    oriented.close()
    return destination, source_width, source_height


def _rotate_source_image_in_place(source_path: Path, rotation_degrees: int) -> tuple[int, int]:
    """Keep the stored scan in the same upright frame as corrected OCR geometry."""

    with tempfile.NamedTemporaryFile(
        prefix=f".{source_path.stem}-upright-",
        suffix=source_path.suffix,
        dir=source_path.parent,
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
    try:
        _rotate_ocr_image_copy(source_path, temporary_path, rotation_degrees)
        with Image.open(temporary_path) as rotated:
            source_width, source_height = rotated.size
        temporary_path.replace(source_path)
        return source_width, source_height
    finally:
        temporary_path.unlink(missing_ok=True)


def scale_ocr_geometry(
    result: OcrResult,
    *,
    normalized_width: int,
    normalized_height: int,
    source_width: int,
    source_height: int,
) -> None:
    """Map OCR pixel geometry back onto the full-resolution source image."""
    scale_x = source_width / max(normalized_width, 1)
    scale_y = source_height / max(normalized_height, 1)
    for word in result.positioned_words:
        word.left *= scale_x
        word.width *= scale_x
        word.top *= scale_y
        word.height *= scale_y
    for layout in result.paragraph_layouts:
        if layout is None:
            continue
        layout.source_width *= scale_x
        layout.left *= scale_x
        layout.right *= scale_x
        layout.top *= scale_y
        layout.bottom *= scale_y
        for fragment in layout.fragments:
            fragment.left *= scale_x
            fragment.width *= scale_x
            fragment.top *= scale_y
            fragment.height *= scale_y
    for layout in result.table_layouts:
        layout.source_width *= scale_x
        layout.source_left *= scale_x
        layout.source_right *= scale_x
        layout.column_widths = [width * scale_x for width in layout.column_widths]
        layout.row_heights = [height * scale_y for height in layout.row_heights]
    for cells in result.table_cells:
        for cell in cells:
            cell.left *= scale_x
            cell.right *= scale_x
            cell.top *= scale_y
            cell.bottom *= scale_y
    result.image_width = source_width
    result.image_height = source_height


def _serialize_paragraph_layout(layout: ParagraphLayout | None) -> dict[str, object] | None:
    return asdict(layout) if layout is not None else None


def _deserialize_paragraph_layout(value: object) -> ParagraphLayout | None:
    if not isinstance(value, dict):
        return None

    fragments: list[TextFragment] = []
    for item in value.get("fragments", []):
        if not isinstance(item, dict):
            continue
        try:
            fragments.append(
                TextFragment(
                    text=str(item.get("text", "")),
                    left=float(item.get("left", 0)),
                    top=float(item.get("top", 0)),
                    width=float(item.get("width", 0)),
                    height=float(item.get("height", 0)),
                )
            )
        except (TypeError, ValueError):
            continue
    try:
        return ParagraphLayout(
            fragments=fragments,
            source_width=float(value.get("source_width", 0)),
            left=float(value.get("left", 0)),
            top=float(value.get("top", 0)),
            right=float(value.get("right", 0)),
            bottom=float(value.get("bottom", 0)),
        )
    except (TypeError, ValueError):
        return None


def _deserialize_positioned_words(value: object) -> list[PositionedWord]:
    if not isinstance(value, list):
        return []
    result: list[PositionedWord] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            result.append(
                PositionedWord(
                    text=str(item.get("text", "")),
                    left=float(item.get("left", 0)),
                    top=float(item.get("top", 0)),
                    width=float(item.get("width", 0)),
                    height=float(item.get("height", 0)),
                    is_form_field=bool(item.get("is_form_field", False)),
                )
            )
        except (TypeError, ValueError):
            continue
    return result


def _validate_positioned_word_bounds(
    words: list[PositionedWordRequest],
    source_width: int,
    source_height: int,
) -> None:
    """Reject edited OCR boxes that cannot belong to the source page."""

    if source_width <= 0 or source_height <= 0:
        return
    tolerance = 3.0  # Allow the editor's CSS pixel rounding at page edges.
    if any(
        word.left > source_width + tolerance
        or word.top > source_height + tolerance
        or word.left + word.width > source_width + tolerance
        or word.top + word.height > source_height + tolerance
        for word in words
    ):
        raise HTTPException(
            status_code=422,
            detail="编辑文字位置超出原图范围，请刷新编辑器后重试",
        )


def _apply_table_cell_edits(
    words: list[PositionedWord],
    edits: list[tuple[TableCell, str]],
) -> list[PositionedWord]:
    """Mirror edits made in the native table view into source-coordinate words."""

    updated = [
        PositionedWord(
            text=word.text,
            left=word.left,
            top=word.top,
            width=word.width,
            height=word.height,
            is_form_field=word.is_form_field,
        )
        for word in words
    ]
    for cell, text in edits:
        if cell.right <= cell.left or cell.bottom <= cell.top:
            continue
        matching = []
        for index, word in enumerate(updated):
            center_x = word.left + word.width / 2.0
            center_y = word.top + word.height / 2.0
            if cell.left <= center_x <= cell.right and cell.top <= center_y <= cell.bottom:
                matching.append(index)
        if matching:
            first = matching[0]
            base_word = updated[first]
            edited_text = clean_ocr_text(text)
            available_width = max(cell.right - base_word.left, base_word.width)
            estimated_lines = max(
                1,
                (max(len(edited_text), 1) * max(base_word.height, 1.0) + available_width - 1)
                // max(available_width, 1.0),
            )
            available_height = max(cell.bottom - base_word.top, base_word.height)
            edited_height = min(
                available_height,
                max(base_word.height, base_word.height * estimated_lines),
            )
            updated[first] = PositionedWord(
                text=edited_text,
                left=base_word.left,
                top=base_word.top,
                width=available_width,
                height=edited_height,
                is_form_field=base_word.is_form_field,
            )
            for index in matching[1:]:
                updated[index] = PositionedWord(
                    text="",
                    left=updated[index].left,
                    top=updated[index].top,
                    width=updated[index].width,
                    height=updated[index].height,
                    is_form_field=updated[index].is_form_field,
                )
        else:
            edited_text = clean_ocr_text(text)
            if edited_text:
                typical_heights = sorted(
                    word.height for word in updated if word.height > 0
                )
                base_height = typical_heights[len(typical_heights) // 2] if typical_heights else 18.0
                available_width = max(cell.right - cell.left - 6.0, 12.0)
                estimated_lines = max(
                    1,
                    int(
                        len(edited_text) * base_height / available_width
                        + 0.999
                    ),
                )
                available_height = max(cell.bottom - cell.top - 4.0, base_height)
                edited_height = min(
                    available_height,
                    max(base_height * 1.2, estimated_lines * base_height * 1.1),
                )
                updated.append(
                    PositionedWord(
                        text=edited_text,
                        left=cell.left + 3.0,
                        top=cell.top + 2.0,
                        width=available_width,
                        height=edited_height,
                    )
                )
    return updated


def _sync_table_text_from_positioned_words(
    tables: list[list[list[str]]],
    table_cells: list[list[TableCell]],
    source_words: list[PositionedWord],
    edited_words: list[PositionedWord],
) -> list[list[list[str]]]:
    """Keep exported table text in sync with edits made to positioned OCR words."""

    updated_tables = [[list(row) for row in table] for table in tables]
    if not source_words or not edited_words:
        return updated_tables

    edited_index = _coordinate_word_index(edited_words)
    for table_index, cells in enumerate(table_cells):
        if table_index >= len(updated_tables):
            continue
        table = updated_tables[table_index]
        for cell in cells:
            if not (0 <= cell.row_start < len(table)):
                continue
            if not (0 <= cell.col_start < len(table[cell.row_start])):
                continue
            source_cell_words = _positioned_words_in_cell(cell, source_words)
            if not source_cell_words:
                continue

            current_cell_words: list[tuple[int, PositionedWord]] = []
            has_word_edit = False
            for source_index, source_word in source_cell_words:
                edited_index_for_word = _first_coordinate_word_match(
                    source_word,
                    edited_words,
                    edited_index,
                )
                if edited_index_for_word is None:
                    continue
                edited_word = edited_words[edited_index_for_word]
                current_cell_words.append((source_index, edited_word))
                has_word_edit = has_word_edit or (
                    clean_ocr_text(edited_word.text) != clean_ocr_text(source_word.text)
                )

            current_text = _join_cell_words(current_cell_words)
            existing_text = clean_ocr_text(table[cell.row_start][cell.col_start])
            if current_cell_words and (has_word_edit or not existing_text):
                table[cell.row_start][cell.col_start] = current_text
    return updated_tables


def _apply_paragraph_edits(
    words: list[PositionedWord],
    original_paragraphs: list[str],
    edited_paragraphs: list[str],
    paragraph_styles: list[str] | None = None,
) -> list[PositionedWord]:
    """Mirror title/header edits from the normal editor into OCR coordinates."""

    updated = [
        PositionedWord(
            text=word.text,
            left=word.left,
            top=word.top,
            width=word.width,
            height=word.height,
            is_form_field=word.is_form_field,
        )
        for word in words
    ]
    for index, original_value in enumerate(original_paragraphs):
        if index >= len(edited_paragraphs):
            continue
        original_text = clean_ocr_text(original_value)
        edited_text = clean_ocr_text(edited_paragraphs[index])
        if not original_text or original_text == edited_text:
            continue
        form_header_markers = ("学校", "年级", "班别", "班级", "学（籍）号", "学籍号")
        if (
            any(word.is_form_field for word in updated)
            and sum(marker in original_text for marker in form_header_markers) >= 2
        ):
            # Fill-in values are stored as positioned field words. Do not
            # fold the whole header line into a single OCR word box.
            continue
        matching = [
            word_index
            for word_index, word in enumerate(updated)
            if clean_ocr_text(word.text) == original_text
        ]
        if not matching:
            continue
        first = matching[0]
        base_word = updated[first]
        length_ratio = max(
            len(edited_text), 1
        ) / max(len(original_text), 1)
        replacement_width = max(
            base_word.width,
            min(base_word.width * 2.5, base_word.width * length_ratio * 1.1),
        )
        style = (
            paragraph_styles[index]
            if paragraph_styles is not None and index < len(paragraph_styles)
            else "body"
        )
        replacement_left = base_word.left
        if style in {"title", "subtitle"}:
            replacement_left = max(
                0.0,
                base_word.left + base_word.width / 2.0 - replacement_width / 2.0,
            )
        updated[first] = PositionedWord(
            text=edited_text,
            left=replacement_left,
            top=base_word.top,
            width=replacement_width,
            height=base_word.height,
            is_form_field=base_word.is_form_field,
        )
        for duplicate in matching[1:]:
            word = updated[duplicate]
            updated[duplicate] = PositionedWord(
                text="",
                left=word.left,
                top=word.top,
                width=word.width,
                height=word.height,
                is_form_field=word.is_form_field,
            )
    return updated


def _effective_paragraph_styles(
    paragraphs: list[str],
    paragraph_styles: list[str] | None,
    has_tables: bool,
) -> list[str]:
    """Fill style gaps in legacy table metadata before exporting edits."""

    styles = list(paragraph_styles or [])
    if len(styles) < len(paragraphs):
        styles.extend(["body"] * (len(paragraphs) - len(styles)))
    if not has_tables:
        return styles
    for index, text in enumerate(paragraphs):
        if styles[index] == "body" and ("申请表" in text or "比赛结果" in text):
            styles[index] = "title"
    result_heading_index = next(
        (index for index, text in enumerate(paragraphs) if "比赛结果" in text),
        None,
    )
    if result_heading_index is not None:
        # Competition sheets put the main title immediately before the
        # subtitle containing “比赛结果”; legacy metadata often omitted both
        # style entries, so include that preceding heading too.
        for index in range(result_heading_index):
            if paragraphs[index].strip():
                styles[index] = "title"
    return styles


def _center_coordinate_heading_edits(
    original_words: list[PositionedWord],
    edited_words: list[PositionedWord],
    paragraphs: list[str],
    paragraph_styles: list[str],
    table_cells: list[list[TableCell]] | None = None,
    source_width: float = 0.0,
    source_height: float = 0.0,
) -> list[PositionedWord]:
    """Center heading edits submitted by the coordinate overlay."""

    heading_texts = {
        clean_ocr_text(text)
        for text, style in zip(paragraphs, paragraph_styles)
        if (
            style in {"title", "subtitle"}
            or (style == "body" and ("申请表" in text or "比赛结果" in text))
        )
        and clean_ocr_text(text)
    }
    if not heading_texts:
        return edited_words

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
    for original in original_words:
        original_text = clean_ocr_text(original.text)
        if original_text not in heading_texts:
            continue
        matching = [
            index
            for index, word in enumerate(updated)
            if abs(word.left - original.left) <= 1.5
            and abs(word.top - original.top) <= 1.5
        ]
        if not matching:
            continue
        index = matching[0]
        current = updated[index]
        if clean_ocr_text(current.text) == original_text:
            continue
        length_ratio = max(len(clean_ocr_text(current.text)), 1) / max(len(original_text), 1)
        replacement_width = max(
            original.width,
            min(original.width * 2.5, original.width * length_ratio * 1.1),
        )
        left_limit, right_limit = _coordinate_word_horizontal_bounds(
            original,
            updated,
            table_cells or [],
            source_width,
            index,
        )
        center_x = original.left + original.width / 2.0
        available_width = max(
            0.0,
            2.0 * min(center_x - left_limit, right_limit - center_x),
        )
        if available_width < original.width:
            # The source box itself is already flush against a safe boundary;
            # preserve its geometry rather than shifting it out of the page.
            replacement_width = original.width
            left = original.left
        else:
            replacement_width = min(replacement_width, available_width)
            left = center_x - replacement_width / 2.0
        candidate = PositionedWord(
            text=current.text,
            left=left,
            top=current.top,
            width=replacement_width,
            height=original.height,
            is_form_field=current.is_form_field,
        )
        _, bottom_limit = _coordinate_word_vertical_bounds(
            original,
            candidate,
            updated,
            table_cells or [],
            source_height,
            index,
        )
        available_height = max(original.height, bottom_limit - original.top)
        height = min(max(original.height, current.height), available_height)
        updated[index] = PositionedWord(
            text=current.text,
            left=left,
            top=current.top,
            width=replacement_width,
            height=height,
            is_form_field=current.is_form_field,
        )
    return updated


def _sync_paragraphs_from_positioned_words(
    original_paragraphs: list[str],
    requested_paragraphs: list[str],
    original_words: list[PositionedWord],
    edited_words: list[PositionedWord],
) -> list[str]:
    """Keep title/subtitle metadata in sync with direct coordinate editing."""

    synced = list(requested_paragraphs)
    if len(synced) < len(original_paragraphs):
        synced.extend(original_paragraphs[len(synced):])
    for paragraph_index, original_value in enumerate(original_paragraphs):
        original_text = clean_ocr_text(original_value)
        if not original_text or paragraph_index >= len(synced):
            continue
        # An explicit edit in the normal paragraph layer already has priority.
        if clean_ocr_text(synced[paragraph_index]) != original_text:
            continue
        for word_index, original_word in enumerate(original_words):
            if clean_ocr_text(original_word.text) != original_text:
                continue
            if word_index >= len(edited_words):
                break
            edited_text = clean_ocr_text(edited_words[word_index].text)
            if edited_text != original_text:
                synced[paragraph_index] = edited_text
            break
    return synced


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "source_fingerprint": BACKEND_SOURCE_FINGERPRINT,
        "api_compatibility_version": API_COMPATIBILITY_VERSION,
        "max_upload_bytes": max_upload_bytes(),
        "max_source_image_bytes": max_source_image_bytes(),
    }


@app.get("/api/v1/assistant/status")
def get_assistant_status() -> dict[str, object]:
    return assistant_status()


@app.post("/api/v1/assistant/chat", response_model=AssistantChatResponse)
async def assistant_chat(payload: AssistantChatRequest) -> AssistantChatResponse:
    messages = [message.model_dump() for message in payload.messages]
    if payload.stream:
        if not assistant_status()["configured"]:
            raise HTTPException(
                status_code=503,
                detail="AI 助手尚未配置。请在后端环境设置 AI_API_KEY 和 AI_MODEL 后重启后端。",
            )

        async def events():
            try:
                reply_stream = (
                    stream_reply(payload.mode, messages, web_search=True)
                    if payload.web_search
                    else stream_reply(payload.mode, messages)
                )
                async for content in reply_stream:
                    yield json.dumps({"type": "delta", "content": content}, ensure_ascii=False) + "\n"
                yield json.dumps({"type": "done"}, ensure_ascii=False) + "\n"
            except AssistantConfigurationError as exc:
                yield json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False) + "\n"
            except AssistantProviderError as exc:
                yield json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False) + "\n"

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        content = (
            await generate_reply(payload.mode, messages, web_search=True)
            if payload.web_search
            else await generate_reply(payload.mode, messages)
        )
    except AssistantConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AssistantProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    config = assistant_status()
    return AssistantChatResponse(
        content=content,
        provider=str(config["provider"]),
        model=str(config["model"]),
    )


@app.post("/api/v1/assistant/export/{file_format}")
def export_assistant_document(file_format: str, payload: AssistantExportRequest):
    if file_format not in {"docx", "pdf"}:
        raise HTTPException(status_code=404, detail="不支持的导出格式")

    temporary = tempfile.TemporaryDirectory(prefix="iscan-assistant-export-")
    output_dir = Path(temporary.name)
    docx_path = output_dir / "iscan-document.docx"
    try:
        create_assistant_docx(payload.title.strip() or "未命名文档", payload.content, docx_path)
        if file_format == "docx":
            output_path = docx_path
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            output_path, _ = convert_docx_to_pdf(docx_path, output_dir)
            if output_path is None or not output_path.exists():
                raise HTTPException(status_code=503, detail="服务器暂时无法生成 PDF，请下载 Word 后另存为 PDF。")
            media_type = "application/pdf"
    except HTTPException:
        temporary.cleanup()
        raise
    except Exception as exc:
        temporary.cleanup()
        raise HTTPException(status_code=500, detail="文档生成失败，请稍后重试。") from exc

    return FileResponse(
        output_path,
        media_type=media_type,
        filename=output_path.name,
        background=BackgroundTask(temporary.cleanup),
    )


@app.get("/api/v1/documents/{document_id}/source-image")
def get_source_image(document_id: str, original: bool = False):
    metadata_file = metadata_path(document_id)
    if not metadata_file.exists():
        raise HTTPException(status_code=404, detail="原图不存在")
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="原图信息不存在") from exc
    # The coordinate editor positions editable OCR text on top of this image.
    # Returning a previously rendered edit here would draw that text twice and
    # make the original-page preview cease to be the imported source.
    name = metadata.get("source_image_file")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=404, detail="原图不存在")
    local_path = storage.path(Path(name).name)
    # OSS 模式下失败任务可能留下 0 字节占位文件；不能把它当成原图
    # 返回给手机，否则整页标题和表格会在编辑页消失。
    if is_non_empty_file(local_path):
        media_type = "image/png" if local_path.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(local_path, media_type=media_type, filename=local_path.name)
    if storage._remote:
        return RedirectResponse(storage.published_url(local_path.name))
    raise HTTPException(status_code=404, detail="原图文件不存在")


@app.get("/api/v1/documents/{document_id}/download/{file_kind}")
def download_document_file(document_id: str, file_kind: str):
    """Stream document downloads through iscan so the browser never opens OSS."""

    metadata_file = metadata_path(document_id)
    if not metadata_file.exists():
        raise HTTPException(status_code=404, detail="文档不存在")
    metadata_keys = {
        "docx": "docx_file",
        "structured-docx": "structured_docx_file",
        "pdf": "pdf_file",
        "structured-pdf": "structured_pdf_file",
    }
    metadata_key = metadata_keys.get(file_kind)
    if metadata_key is None:
        raise HTTPException(status_code=404, detail="不支持的下载格式")
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="文档信息不存在") from exc
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=404, detail="文档信息不存在")
    download_name = _document_download_filename(document_id, file_kind, metadata)
    stored_name = metadata.get(metadata_key)
    if file_kind == "docx" and not stored_name:
        stored_name = f"{document_id}.docx"
    if not isinstance(stored_name, str) or not stored_name:
        raise HTTPException(status_code=404, detail="文件不存在")
    file_name = Path(stored_name).name
    expected_suffix = ".pdf" if file_kind in {"pdf", "structured-pdf"} else ".docx"
    if file_name != stored_name or Path(file_name).suffix.lower() != expected_suffix:
        raise HTTPException(status_code=404, detail="文件不存在")

    local_path = storage.path(file_name)
    media_type = (
        "application/pdf"
        if file_kind in {"pdf", "structured-pdf"}
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    if is_non_empty_file(local_path):
        return FileResponse(
            local_path,
            media_type=media_type,
            filename=download_name,
            headers={"Cache-Control": "no-store"},
        )
    if not storage._remote:
        raise HTTPException(status_code=404, detail="文件不存在")

    with tempfile.NamedTemporaryFile(
        prefix="iscan-download-",
        suffix=expected_suffix,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        storage.download_published(file_name, temporary_path)
        if not is_non_empty_file(temporary_path):
            raise FileNotFoundError(file_name)
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        logger.warning(
            "Document download failed (document_id=%s, kind=%s, error_type=%s)",
            document_id,
            file_kind,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="文件暂时无法下载，请稍后重试") from exc
    return FileResponse(
        temporary_path,
        media_type=media_type,
        filename=download_name,
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(temporary_path.unlink, missing_ok=True),
    )


@app.get("/api/v1/documents/{document_id}/refresh", response_model=DocumentResponse)
def refresh_document(request: Request, document_id: str) -> DocumentResponse:
    """Return fresh signed file URLs for a locally cached document."""

    has_positioned_word_baseline = False
    metadata_file = metadata_path(document_id)
    if not metadata_file.exists():
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        document_warnings = _stored_document_warnings(metadata.get("warnings"))
        layouts = [TableLayout(**value) for value in metadata.get("table_layouts", [])]
        cells = [
            [TableCell(**cell) for cell in table_cells]
            for table_cells in metadata.get("table_cells", [])
        ]
        paragraph_layouts = [
            _deserialize_paragraph_layout(value)
            for value in metadata.get("paragraph_layouts", [])
        ]
        positioned_words = _deserialize_positioned_words(metadata.get("positioned_words", []))
        stored_source_words = metadata.get("source_positioned_words")
        has_positioned_word_baseline = isinstance(stored_source_words, list)
        source_positioned_words = (
            _deserialize_positioned_words(stored_source_words)
            if isinstance(stored_source_words, list)
            else positioned_words
        )
        stored_source_cells = metadata.get("source_table_cells")
        if isinstance(stored_source_cells, list):
            source_table_cells = [
                [TableCell(**cell) for cell in table_cells]
                for table_cells in stored_source_cells
            ]
        else:
            source_table_cells = recover_source_table_cells(
                cells,
                source_positioned_words,
                positioned_words,
            )
        image_width, image_height = _stored_image_dimensions(document_id, metadata)
        stored_paper_colors = metadata.get("source_paper_colors")
        paper_colors = stored_paper_colors if isinstance(stored_paper_colors, dict) else {}
        result = OcrResult(
            image_width=image_width,
            image_height=image_height,
            paragraphs=[str(value) for value in metadata.get("paragraphs", [])],
            paragraph_styles=[str(value) for value in metadata.get("paragraph_styles", [])],
            paragraph_layouts=paragraph_layouts,
            positioned_words=positioned_words,
            tables=metadata.get("tables", []),
            table_layouts=layouts,
            table_cells=cells,
            low_confidence_examples=_stored_low_confidence_examples(
                metadata.get("low_confidence_examples"),
                metadata.get("warnings"),
            ),
        )
        if result.tables and normalize_table_cell_text(
            result,
            source_table_cells,
            source_positioned_words,
        ):
            metadata["tables"] = result.tables
            metadata["table_cells"] = [
                [asdict(cell) for cell in table_cells]
                for table_cells in result.table_cells
            ]
            try:
                metadata_file.write_text(
                    json.dumps(metadata, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                logger.warning(
                    "Could not persist normalized table text for document %s",
                    document_id,
                )
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="文档元数据损坏") from exc

    plain_text_migration_needed = False
    if not result.tables:
        had_source_geometry = reflow_plain_text_result(result)
        positioned_words = result.positioned_words
        paragraph_layouts = result.paragraph_layouts
        if not has_positioned_word_baseline:
            source_positioned_words = list(positioned_words)
        plain_text_migration_needed = bool(
            any(clean_ocr_text(value) for value in result.paragraphs)
            and (
                metadata.get("plain_text_layout_version") != PLAIN_TEXT_LAYOUT_VERSION
                or had_source_geometry
            )
        )
    needs_visual_source_refresh = bool(
        result.tables or source_positioned_words or not has_positioned_word_baseline
    )

    source_url = source_image_endpoint(request, document_id)
    original_source_url = source_image_endpoint(request, document_id, original=True)
    docx_name = str(metadata.get("docx_file") or f"{document_id}.docx")
    # Resolve the source page once for both visual and structured backfills.
    # Previously this lived only inside the visual-layout branch; when the
    # visual version was already current, the structured refresh referenced an
    # unbound source_path and silently returned no structured Word URL.
    source_name = metadata.get("source_image_file")
    source_path: Path | None = None
    temporary_source: Path | None = None
    if (
        needs_visual_source_refresh
        and isinstance(source_name, str)
        and source_name
    ):
        source_path = storage.path(Path(source_name).name)
        if not is_non_empty_file(source_path):
            temporary_source = storage.path(
                f"{document_id}_refresh_source{_source_image_suffix(source_name)}"
            )
            try:
                storage.download_published(source_name, temporary_source)
                source_path = temporary_source
            except Exception:
                source_path = None

    if plain_text_migration_needed:
        plain_docx_name = f"{document_id}_plain_text_v{PLAIN_TEXT_LAYOUT_VERSION}.docx"
        plain_docx_path = storage.path(plain_docx_name)
        regenerated_pdf: Path | None = None
        try:
            create_native_table_docx(
                result,
                plain_docx_path,
                title=_metadata_document_title(metadata, document_id),
            )
            if not is_non_empty_file(plain_docx_path):
                raise OSError("plain-text DOCX generation produced no file")
        except Exception:
            plain_docx_path.unlink(missing_ok=True)
            logger.exception(
                "Structured plain-text document refresh failed for %s",
                document_id,
            )
            warning = TEXT_STRUCTURED_DOCX_UNAVAILABLE_WARNING
            if warning not in document_warnings:
                document_warnings.append(warning)
        else:
            try:
                regenerated_pdf, used_image_fallback = convert_docx_to_pdf(
                    plain_docx_path,
                    storage.root,
                )
                if (
                    regenerated_pdf is None
                    or not is_non_empty_file(regenerated_pdf)
                    or used_image_fallback
                ):
                    if regenerated_pdf is not None:
                        regenerated_pdf.unlink(missing_ok=True)
                    regenerated_pdf = None
            except Exception:
                logger.exception(
                    "Could not generate plain-text PDF refresh for %s",
                    document_id,
                )
                regenerated_pdf = None

            plain_pdf_name: str | None = None
            try:
                storage.publish(plain_docx_path)
            except Exception:
                plain_docx_path.unlink(missing_ok=True)
                if regenerated_pdf is not None:
                    regenerated_pdf.unlink(missing_ok=True)
                logger.exception(
                    "Could not publish structured plain-text DOCX refresh for %s",
                    document_id,
                )
                warning = TEXT_STRUCTURED_DOCX_UNAVAILABLE_WARNING
                if warning not in document_warnings:
                    document_warnings.append(warning)
            else:
                if regenerated_pdf is not None:
                    try:
                        storage.publish(regenerated_pdf)
                        plain_pdf_name = regenerated_pdf.name
                    except Exception:
                        regenerated_pdf.unlink(missing_ok=True)
                        logger.exception(
                            "Copyable plain-text PDF refresh failed for %s",
                            document_id,
                        )

                metadata["structured_docx_file"] = plain_docx_name
                metadata["structured_docx_layout_version"] = STRUCTURED_DOCX_LAYOUT_VERSION
                metadata["structured_pdf_file"] = plain_pdf_name
                metadata["structured_pdf_layout_version"] = (
                    TABLE_PDF_LAYOUT_VERSION if plain_pdf_name is not None else None
                )
                metadata["paragraphs"] = result.paragraphs
                metadata["paragraph_styles"] = result.paragraph_styles
                metadata["paragraph_layouts"] = [
                    _serialize_paragraph_layout(layout)
                    for layout in result.paragraph_layouts
                ]
                metadata["positioned_words"] = [
                    _serialize_positioned_word(word)
                    for word in result.positioned_words
                ]
                metadata["plain_text_layout_version"] = PLAIN_TEXT_LAYOUT_VERSION
                document_warnings = [
                    warning
                    for warning in document_warnings
                    if warning != "纯文字 Word 已更新，PDF 暂未重新生成"
                ]
                if TEXT_STRUCTURED_DOCX_WARNING not in document_warnings:
                    document_warnings.append(TEXT_STRUCTURED_DOCX_WARNING)
                if plain_pdf_name is None:
                    if TABLE_TEXT_PDF_UNAVAILABLE_WARNING not in document_warnings:
                        document_warnings.append(TABLE_TEXT_PDF_UNAVAILABLE_WARNING)
                elif TABLE_TEXT_PDF_WARNING not in document_warnings:
                    document_warnings.append(TABLE_TEXT_PDF_WARNING)
                metadata["warnings"] = _stored_document_warnings(document_warnings)
                try:
                    metadata_file.write_text(
                        json.dumps(metadata, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except OSError:
                    logger.exception(
                        "Could not persist structured plain-text migration for %s",
                        document_id,
                    )
                    document_warnings.append(TEXT_STRUCTURED_DOCX_UNAVAILABLE_WARNING)

    if needs_visual_source_refresh and (
        metadata.get("visual_docx_layout_version") != VISUAL_DOCX_LAYOUT_VERSION
    ):
        if source_path is not None and is_non_empty_file(source_path):
            visual_path = storage.path(Path(docx_name).name)
            temporary_visual_path = storage.path(f"{document_id}_refresh_visual.docx")
            try:
                # A visual-layout backfill must preserve saved OCR edits. If
                # no edits exist, keep the image-only copy to avoid drawing
                # duplicate OCR text over the scan. Some older table editors
                # only persisted cell text, so check both representations.
                has_table_cell_edits = len(cells) != len(source_table_cells) or any(
                    len(current_cells) != len(source_cells)
                    or any(
                        current.text != source.text
                        for current, source in zip(current_cells, source_cells)
                    )
                    for current_cells, source_cells in zip(cells, source_table_cells)
                )
                has_visual_edits = (
                    _has_positioned_word_changes(source_positioned_words, positioned_words)
                    or has_table_cell_edits
                )
                # Rebuild the same visual file name so existing clients can
                # keep using the refreshed docx_url without special handling.
                # Write to a temporary file first so a failed rebuild never
                # destroys the older document that is still downloadable.
                create_docx(
                    result,
                    temporary_visual_path,
                    source_path,
                    original_words=(source_positioned_words if has_visual_edits else None),
                    original_table_cells=source_table_cells,
                    source_positioned_words=source_positioned_words,
                    preserve_source_layout=True,
                    title=_metadata_document_title(metadata, document_id),
                )
                temporary_visual_path.replace(visual_path)
                storage.publish(visual_path)
                metadata["visual_docx_layout_version"] = VISUAL_DOCX_LAYOUT_VERSION
                metadata_file.write_text(
                    json.dumps(metadata, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                temporary_visual_path.unlink(missing_ok=True)
    structured_docx_name = metadata.get("structured_docx_file")
    if (result.tables or _ocr_result_has_content(result)) and (
        not structured_docx_name
        or metadata.get("structured_docx_layout_version") != STRUCTURED_DOCX_LAYOUT_VERSION
    ):
        # Layout upgrades regenerate structured text and native-table output
        # from OCR metadata instead of leaving a stale cached DOCX in history.
        backfill_path = storage.path(f"{document_id}_structured.docx")
        try:
            if result.tables and (
                source_path is None or not is_non_empty_file(source_path)
            ):
                # Do not replace an existing structured file with a degraded
                # fixed native table when the original scan is unavailable.
                if structured_docx_name:
                    raise FileNotFoundError("表格原图不可用，保留已有结构 Word")
                raise FileNotFoundError("表格原图不可用，无法生成结构 Word")
            # Use the scan only for page geometry and checkbox positions. The
            # structured Word itself is built from editable native table cells.
            create_native_table_docx(
                result,
                backfill_path,
                source_path,
                title=_metadata_document_title(metadata, document_id),
            )
            storage.publish(backfill_path)
            structured_docx_name = backfill_path.name
            metadata["structured_docx_file"] = structured_docx_name
            metadata["structured_docx_layout_version"] = STRUCTURED_DOCX_LAYOUT_VERSION
            metadata_file.write_text(
                json.dumps(metadata, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            backfill_path.unlink(missing_ok=True)
            # Keep a previously published file address if the rebuild failed;
            # a transient refresh failure should not make an existing Word
            # option disappear from the response.
            structured_docx_name = metadata.get("structured_docx_file")
    if needs_visual_source_refresh and (
        metadata.get("visual_pdf_layout_version") != VISUAL_PDF_LAYOUT_VERSION
        or metadata.get("structured_pdf_layout_version") != TABLE_PDF_LAYOUT_VERSION
    ):
        visual_pdf_name = metadata.get("pdf_file")
        structured_pdf_name = metadata.get("structured_pdf_file")
        if metadata.get("visual_pdf_layout_version") != VISUAL_PDF_LAYOUT_VERSION:
            visual_docx_source: Path | None = None
            if isinstance(docx_name, str) and docx_name:
                candidate = storage.path(Path(docx_name).name)
                if not is_non_empty_file(candidate) and storage._remote:
                    try:
                        storage.download_published(docx_name, candidate)
                    except Exception:
                        candidate = None
                if candidate is not None and is_non_empty_file(candidate):
                    visual_docx_source = candidate
            try:
                if visual_docx_source is not None:
                    if source_path is not None and is_non_empty_file(source_path):
                        with tempfile.TemporaryDirectory(
                            prefix="iscan-refresh-visual-pdf-",
                            dir=storage.root,
                        ) as temporary_directory:
                            visual_image = Path(temporary_directory) / "edited.png"
                            render_positioned_image(
                                result,
                                source_path,
                                visual_image,
                                original_words=source_positioned_words,
                            )
                            regenerated_visual_pdf, _ = convert_docx_to_pdf(
                                visual_docx_source,
                                storage.root,
                                fallback_image_path=visual_image,
                                prefer_image=True,
                            )
                    else:
                        regenerated_visual_pdf, _ = convert_docx_to_pdf(
                            visual_docx_source,
                            storage.root,
                        )
                    if regenerated_visual_pdf is not None:
                        storage.publish(regenerated_visual_pdf)
                        visual_pdf_name = regenerated_visual_pdf.name
                        metadata["pdf_file"] = visual_pdf_name
                        metadata["visual_pdf_layout_version"] = VISUAL_PDF_LAYOUT_VERSION
            except Exception as exc:
                logger.warning(
                    "Visual PDF refresh failed for document %s (error_type=%s)",
                    document_id,
                    type(exc).__name__,
                )

        if (
            metadata.get("structured_docx_layout_version")
            == STRUCTURED_DOCX_LAYOUT_VERSION
            and metadata.get("structured_pdf_layout_version")
            != TABLE_PDF_LAYOUT_VERSION
        ):
            structured_pdf_source: Path | None = None
            if isinstance(structured_docx_name, str) and structured_docx_name:
                candidate = storage.path(Path(structured_docx_name).name)
                if not is_non_empty_file(candidate) and storage._remote:
                    try:
                        storage.download_published(structured_docx_name, candidate)
                    except Exception:
                        candidate = None
                if candidate is not None and is_non_empty_file(candidate):
                    structured_pdf_source = candidate
            try:
                if structured_pdf_source is not None:
                    regenerated_structured_pdf, used_image_fallback = convert_docx_to_pdf(
                        structured_pdf_source,
                        storage.root,
                    )
                    if (
                        regenerated_structured_pdf is not None
                        and not used_image_fallback
                    ):
                        storage.publish(regenerated_structured_pdf)
                        structured_pdf_name = regenerated_structured_pdf.name
                        metadata["structured_pdf_file"] = structured_pdf_name
                        metadata["structured_pdf_layout_version"] = TABLE_PDF_LAYOUT_VERSION
            except Exception as exc:
                logger.warning(
                    "Searchable table PDF refresh failed for document %s "
                    "(error_type=%s)",
                    document_id,
                    type(exc).__name__,
                )

        visual_pdf_is_current = (
            metadata.get("visual_pdf_layout_version") == VISUAL_PDF_LAYOUT_VERSION
        )
        structured_pdf_is_current = (
            metadata.get("structured_pdf_layout_version") == TABLE_PDF_LAYOUT_VERSION
        )
        table_pdf_warnings = []
        if visual_pdf_is_current and visual_pdf_name:
            table_pdf_warnings.append(TABLE_IMAGE_PDF_WARNING)
        else:
            table_pdf_warnings.append(TABLE_PDF_UNAVAILABLE_WARNING)
        if structured_pdf_is_current and structured_pdf_name:
            table_pdf_warnings.append(TABLE_TEXT_PDF_WARNING)
        else:
            table_pdf_warnings.append(TABLE_TEXT_PDF_UNAVAILABLE_WARNING)
        _set_table_pdf_warning(document_warnings, *table_pdf_warnings)
        metadata["warnings"] = _stored_document_warnings(document_warnings)
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    if not paper_colors and source_path is not None and is_non_empty_file(source_path):
        paper_colors = sample_source_paper_colors(
            source_path,
            OcrResult(
                positioned_words=source_positioned_words,
                table_cells=source_table_cells,
            ),
        )
        if paper_colors:
            metadata["source_paper_colors"] = paper_colors
            metadata_file.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    pdf_name = metadata.get("pdf_file")
    pdf_is_current = (
        not needs_visual_source_refresh
        or metadata.get("visual_pdf_layout_version") == VISUAL_PDF_LAYOUT_VERSION
    )
    structured_pdf_name = metadata.get("structured_pdf_file")
    structured_pdf_is_current = (
        metadata.get("structured_pdf_layout_version") == TABLE_PDF_LAYOUT_VERSION
    )
    response = DocumentResponse(
        id=document_id,
        title=_metadata_document_title(metadata, document_id),
        has_tables=bool(result.tables),
        docx_url=document_download_endpoint(document_id, "docx"),
        docx_preserves_source_layout=(
            metadata.get("visual_docx_layout_version") == VISUAL_DOCX_LAYOUT_VERSION
        ),
        structured_docx_url=(
            document_download_endpoint(document_id, "structured-docx")
            if structured_docx_name
            else None
        ),
        pdf_url=(
            document_download_endpoint(document_id, "pdf")
            if pdf_name and pdf_is_current
            else None
        ),
        pdf_preserves_source_layout=(
            metadata.get("visual_pdf_layout_version") == VISUAL_PDF_LAYOUT_VERSION
        ),
        structured_pdf_url=(
            document_download_endpoint(document_id, "structured-pdf")
            if structured_pdf_name and structured_pdf_is_current
            else None
        ),
        warnings=document_warnings,
        paragraphs=result.paragraphs,
        paragraph_styles=result.paragraph_styles,
        tables=result.tables,
        table_layouts=[asdict(layout) for layout in result.table_layouts],
        table_cells=[
            [asdict(cell) for cell in cells]
            for cells in result.table_cells
        ],
        image_width=result.image_width,
        image_height=result.image_height,
        positioned_words=[_serialize_positioned_word(word) for word in positioned_words],
        editor_html=render_editor_html(
            result,
            source_url,
            original_source_url,
            source_positioned_words,
            source_table_cells,
            paper_colors,
            positioned_table_words=metadata.get("positioned_table_words") is True,
        ),
        source_image_url=source_url,
    )
    if temporary_source is not None:
        temporary_source.unlink(missing_ok=True)
    return response


@app.post("/api/v1/documents/convert", response_model=DocumentResponse)
async def convert_document(request: Request, image: UploadFile = File(...)) -> DocumentResponse:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="请上传图片文件")

    document_id = uuid.uuid4().hex
    document_title = _safe_document_stem(image.filename)
    raw_path = storage.path(f"{document_id}_raw")
    processed_path = storage.path(f"{document_id}_processed.jpg")
    source_path: Path | None = None
    docx_path = storage.path(f"{document_id}.docx")
    structured_docx_path: Path | None = None
    pdf_path: Path | None = None
    structured_pdf_path: Path | None = None
    used_image_fallback = False
    metadata_file = metadata_path(document_id)
    published_names: list[str] = []
    source_image_url: str | None = None
    warnings: list[str] = []

    def cleanup_failed_conversion() -> None:
        paths = (
            source_path,
            docx_path,
            structured_docx_path,
            pdf_path,
            structured_pdf_path,
            metadata_file,
        )
        for path in paths:
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove partial conversion file %s", path, exc_info=True)
        for name in published_names:
            try:
                storage.delete_published(name)
            except Exception:
                logger.warning("Failed to remove partially published conversion object %s", name, exc_info=True)

    conversion_stage = "save_upload"
    try:
        await save_upload(image, raw_path)
        processed_path = storage.path(
            f"{document_id}_processed{_normalized_ocr_suffix(raw_path)}"
        )
        conversion_stage = "normalize_image"
        normalize_image(raw_path, processed_path)
        conversion_stage = "preserve_source_image"
        source_path, source_width, source_height = preserve_source_image(raw_path, document_id)
        conversion_stage = "ocr"
        provider = get_ocr_provider()
        ocr_result, used_local_ocr_fallback = await _recognize_with_transient_ocr_retry(
            provider,
            processed_path,
            text_source_path=source_path,
        )
        if ocr_result.orientation_correction_degrees:
            conversion_stage = "correct_source_orientation"
            source_width, source_height = _rotate_source_image_in_place(
                source_path,
                ocr_result.orientation_correction_degrees,
            )
        if not _ocr_result_has_content(ocr_result):
            warnings.append(
                "未识别到可编辑文字，已保留原图版 Word；请尝试更清晰、无反光的图片"
            )
        elif used_local_ocr_fallback:
            warnings.append("云端识别未返回可用文字或暂时不可用，已使用本机 OCR；请校对识别文字和表格")
        confidence_warning_index: int | None = None
        if ocr_result.used_general_text_fallback and ocr_result.low_text_confidence:
            warnings.append("已使用通用文字识别，且部分文字置信度偏低，请结合原图校对")
            confidence_warning_index = len(warnings) - 1
        elif ocr_result.used_general_text_fallback:
            warnings.append("已使用通用文字识别结果，请校对识别文字")
        elif ocr_result.low_text_confidence:
            warnings.append("部分文字识别置信度偏低，请结合原图校对")
            confidence_warning_index = len(warnings) - 1
        if confidence_warning_index is not None and ocr_result.low_confidence_examples:
            examples = "、".join(ocr_result.low_confidence_examples)
            warnings[confidence_warning_index] += f"；建议优先核对：{examples}"
        conversion_stage = "read_normalized_dimensions"
        with Image.open(processed_path) as normalized_image:
            normalized_width, normalized_height = normalized_image.size
        if ocr_result.orientation_correction_degrees:
            normalized_width = ocr_result.image_width
            normalized_height = ocr_result.image_height
        elif (
            used_local_ocr_fallback
            and ocr_result.image_width > 0
            and ocr_result.image_height > 0
        ):
            normalized_width = ocr_result.image_width
            normalized_height = ocr_result.image_height
        scale_ocr_geometry(
            ocr_result,
            normalized_width=normalized_width,
            normalized_height=normalized_height,
            source_width=source_width,
            source_height=source_height,
        )
        reflow_plain_text_result(ocr_result)
        source_paper_colors = sample_source_paper_colors(source_path, ocr_result)
        serialized_paragraph_layouts = [
            _serialize_paragraph_layout(layout)
            for layout in ocr_result.paragraph_layouts
        ]
        serialized_positioned_words = [
            _serialize_positioned_word(word) for word in ocr_result.positioned_words
        ]
        serialized_table_layouts = [asdict(layout) for layout in ocr_result.table_layouts]
        serialized_table_cells = [
            [asdict(cell) for cell in cells]
            for cells in ocr_result.table_cells
        ]
        conversion_stage = "write_metadata"
        # At first import, the source and current OCR snapshots are identical.
        # Refresh/edit readers infer the source baseline until the first edit,
        # when both snapshots are stored separately.
        metadata_file.write_text(
            json.dumps(
                {
                    "title": document_title,
                    "image_width": ocr_result.image_width,
                    "image_height": ocr_result.image_height,
                    "paragraphs": ocr_result.paragraphs,
                    "paragraph_styles": ocr_result.paragraph_styles,
                    "paragraph_layouts": serialized_paragraph_layouts,
                    "positioned_words": serialized_positioned_words,
                    "low_confidence_examples": ocr_result.low_confidence_examples,
                    "plain_text_layout_version": (
                        PLAIN_TEXT_LAYOUT_VERSION if not ocr_result.tables else None
                    ),
                    "positioned_table_words": True,
                    "source_image_file": source_path.name,
                    "source_paper_colors": source_paper_colors,
                    "tables": ocr_result.tables,
                    "table_layouts": serialized_table_layouts,
                    "table_cells": serialized_table_cells,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        conversion_stage = "create_docx"
        create_docx(
            ocr_result,
            docx_path,
            source_path,
            preserve_source_layout=True,
            title=document_title,
        )
        if ocr_result.tables or _ocr_result_has_content(ocr_result):
            # 默认 Word 保留原图版式；结构版的文字和边框都由 Word
            # 单元格/段落承载，不再叠加在扫描图上。
            structured_docx_path = storage.path(f"{document_id}_structured.docx")
            try:
                create_native_table_docx(
                    ocr_result,
                    structured_docx_path,
                    source_path,
                    title=document_title,
                )
                warnings.append(
                    TABLE_STRUCTURED_DOCX_WARNING
                    if ocr_result.tables
                    else TEXT_STRUCTURED_DOCX_WARNING
                )
            except Exception as exc:
                logger.exception(
                    "Structured editable Word generation failed; preserving the "
                    "original-layout document (document_id=%s, error_type=%s)",
                    document_id,
                    type(exc).__name__,
                )
                # OCR 返回的跨行/跨列区域有时不是同一个矩形网格，
                # 原图版 Word 仍然是完整可交付结果，不能因可选的原生
                # 表格版本失败而让整次转换返回 500。
                structured_docx_path.unlink(missing_ok=True)
                structured_docx_path = None
                warnings.append(
                    TABLE_STRUCTURED_DOCX_UNAVAILABLE_WARNING
                    if ocr_result.tables
                    else TEXT_STRUCTURED_DOCX_UNAVAILABLE_WARNING
                )
        try:
            # Keep the default PDF faithful to the uploaded scan for both
            # table forms and ordinary text pages. The editable/copyable
            # alternative is generated from the structured DOCX below.
            pdf_path, _ = convert_docx_to_pdf(
                docx_path,
                storage.root,
                fallback_image_path=source_path,
                prefer_image=True,
            )
        except Exception as exc:
            logger.exception(
                "Visual PDF conversion failed for document %s (error_type=%s)",
                document_id,
                type(exc).__name__,
            )
            warnings.append("PDF 转换失败，请稍后重试")
        if structured_docx_path is not None:
            try:
                structured_pdf_path, _ = convert_docx_to_pdf(
                    structured_docx_path,
                    storage.root,
                )
            except Exception as exc:
                logger.exception(
                    "Copyable OCR PDF conversion failed for document %s "
                    "(error_type=%s)",
                    document_id,
                    type(exc).__name__,
                )
        pdf_warnings = []
        if pdf_path is not None:
            pdf_warnings.append(TABLE_IMAGE_PDF_WARNING)
        else:
            pdf_warnings.append(TABLE_PDF_UNAVAILABLE_WARNING)
        if structured_docx_path is not None:
            if structured_pdf_path is not None:
                pdf_warnings.append(TABLE_TEXT_PDF_WARNING)
            else:
                pdf_warnings.append(TABLE_TEXT_PDF_UNAVAILABLE_WARNING)
        _set_table_pdf_warning(warnings, *pdf_warnings)
    except HTTPException as exc:
        cleanup_failed_conversion()
        if _is_image_close_error(exc):
            logger.error(
                "Image conversion failed during %s (document_id=%s, error_type=%s)",
                conversion_stage,
                document_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="图片识别服务暂时处理失败，请稍后重试",
                headers={
                    "X-Conversion-ID": document_id,
                    "X-Error-Code": "OCR_NULL_CLOSE",
                },
            ) from exc
        raise
    except (RuntimeError, httpx.HTTPError) as exc:
        cleanup_failed_conversion()
        logger.error(
            "Image conversion failed during %s (document_id=%s, error_type=%s)",
            conversion_stage,
            document_id,
            type(exc).__name__,
        )
        failure_headers = {"X-Conversion-ID": document_id}
        # httpx.HTTPStatusError includes the full upstream URL in its string;
        # OCR URLs may contain access tokens, so never return that text to the
        # browser. Keep the error type and conversion id in server logs instead.
        detail = (
            "自建 OCR 服务暂时不可用，请稍后重试"
            if isinstance(exc, httpx.HTTPError)
            else "图片识别失败，请稍后重试"
        )
        if _is_image_close_error(exc):
            detail = "图片识别服务暂时处理失败，请稍后重试"
            failure_headers["X-Error-Code"] = "OCR_NULL_CLOSE"
        elif isinstance(exc, httpx.HTTPError) and _is_transient_ocr_error(exc):
            failure_headers["X-Error-Code"] = "OCR_TRANSIENT"
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                failure_headers["Retry-After"] = str(retry_after)
        raise HTTPException(
            status_code=503,
            detail=detail,
            headers=failure_headers,
        ) from exc
    except Exception as exc:
        cleanup_failed_conversion()
        logger.exception(
            "Image conversion failed unexpectedly during %s "
            "(document_id=%s, error_type=%s)",
            conversion_stage,
            document_id,
            type(exc).__name__,
        )
        failure_headers = {"X-Conversion-ID": document_id}
        detail = "图片识别失败，请稍后重试"
        if _is_image_close_error(exc):
            detail = "图片识别服务暂时处理失败，请稍后重试"
            failure_headers["X-Error-Code"] = "OCR_NULL_CLOSE"
        raise HTTPException(
            status_code=503,
            detail=detail,
            headers=failure_headers,
        ) from exc
    finally:
        raw_path.unlink(missing_ok=True)
        processed_path.unlink(missing_ok=True)

    try:
        published_names.append(docx_path.name)
        docx_url = storage.publish(docx_path)
        if structured_docx_path is not None:
            published_names.append(structured_docx_path.name)
        structured_docx_url = (
            storage.publish(structured_docx_path)
            if structured_docx_path is not None
            else None
        )
        if structured_pdf_path:
            published_names.append(structured_pdf_path.name)
        structured_pdf_url = storage.publish(structured_pdf_path) if structured_pdf_path else None
        if pdf_path:
            published_names.append(pdf_path.name)
        pdf_url = storage.publish(pdf_path) if pdf_path else None
        published_names.append(source_path.name)
        storage.publish(source_path, inline=True)
        source_image_url = source_image_endpoint(request, document_id)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata["source_image_url"] = source_image_url
        metadata["docx_file"] = docx_path.name
        metadata["visual_docx_layout_version"] = (
            VISUAL_DOCX_LAYOUT_VERSION if docx_path.is_file() else None
        )
        metadata["structured_docx_file"] = (
            structured_docx_path.name if structured_docx_path is not None else None
        )
        metadata["structured_docx_layout_version"] = (
            STRUCTURED_DOCX_LAYOUT_VERSION if structured_docx_path is not None else None
        )
        metadata["structured_pdf_layout_version"] = (
            TABLE_PDF_LAYOUT_VERSION if structured_pdf_path is not None else None
        )
        metadata["visual_pdf_layout_version"] = (
            VISUAL_PDF_LAYOUT_VERSION if pdf_path is not None else None
        )
        metadata["pdf_file"] = pdf_path.name if pdf_path else None
        metadata["structured_pdf_file"] = (
            structured_pdf_path.name if structured_pdf_path else None
        )
        metadata["warnings"] = _stored_document_warnings(warnings)
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        cleanup_failed_conversion()
        logger.error(
            "Image document publication failed (document_id=%s, error_type=%s)",
            document_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="识别结果暂时无法保存，请稍后重试",
        ) from exc

    return DocumentResponse(
        id=document_id,
        title=document_title,
        has_tables=bool(ocr_result.tables),
        docx_url=document_download_endpoint(document_id, "docx"),
        docx_preserves_source_layout=docx_url is not None,
        structured_docx_url=(
            document_download_endpoint(document_id, "structured-docx")
            if structured_docx_url
            else None
        ),
        pdf_url=document_download_endpoint(document_id, "pdf") if pdf_url else None,
        pdf_preserves_source_layout=pdf_url is not None,
        structured_pdf_url=(
            document_download_endpoint(document_id, "structured-pdf")
            if structured_pdf_url
            else None
        ),
        warnings=warnings,
        paragraphs=ocr_result.paragraphs,
        paragraph_styles=ocr_result.paragraph_styles,
        tables=ocr_result.tables,
        table_layouts=serialized_table_layouts,
        table_cells=serialized_table_cells,
        image_width=ocr_result.image_width,
        image_height=ocr_result.image_height,
        positioned_words=serialized_positioned_words,
        editor_html=render_editor_html(
            ocr_result,
            source_image_url,
            paper_colors=source_paper_colors,
            positioned_table_words=True,
        ),
        source_image_url=source_image_url,
    )


@app.post("/api/v1/documents/export-edited", response_model=DocumentResponse)
async def export_edited_document(
    http_request: Request,
    request: EditedDocumentRequest,
) -> DocumentResponse:
    """用 App 编辑后的文字和表格重新生成可编辑的 Word/PDF。"""
    document_id = uuid.uuid4().hex
    docx_path = storage.path(f"{document_id}.docx")
    structured_docx_path: Path | None = None
    pdf_path: Path | None = None
    structured_pdf_path: Path | None = None
    used_image_fallback = False
    warnings: list[str] = []
    source_image_url: str | None = None
    low_confidence_examples: list[str] = []
    original_cells: list[list[TableCell]] = []
    source_table_cells: list[list[TableCell]] = []
    original_layouts: list[TableLayout] = []
    original_paragraphs: list[str] = []
    paragraph_styles: list[str] = []
    paragraph_layouts: list[ParagraphLayout | None] = []
    positioned_words: list[PositionedWord] = []
    source_positioned_words: list[PositionedWord] = []
    source_image_path: Path | None = None
    source_image_temp: Path | None = None
    rendered_pdf_image_path: Path | None = None
    source_image_file: str | None = None
    source_paper_colors: dict[str, str] = {}
    document_title = _safe_document_stem(request.title) if request.title else "扫描文档"
    positioned_table_words = False
    output_metadata_path: Path | None = None
    published_names: list[str] = []
    image_width = 0
    image_height = 0
    table_cell_edits: list[tuple[TableCell, str]] = []

    def cleanup_failed_export() -> None:
        for path in (
            docx_path,
            structured_docx_path,
            pdf_path,
            structured_pdf_path,
            output_metadata_path,
            source_image_temp,
            rendered_pdf_image_path,
        ):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove partial edited document file %s", path, exc_info=True)
        for name in published_names:
            try:
                storage.delete_published(name)
            except Exception:
                logger.warning("Failed to remove partially published edited document %s", name, exc_info=True)

    if request.document_id:
        source_metadata = metadata_path(request.document_id)
        if source_metadata.exists():
            try:
                metadata = json.loads(source_metadata.read_text(encoding="utf-8"))
                stored_title = metadata.get("title")
                if isinstance(stored_title, str) and stored_title.strip():
                    document_title = _safe_document_stem(stored_title)
                elif not request.title:
                    document_title = _metadata_document_title(metadata, request.document_id)
                warnings = _stored_document_warnings(metadata.get("warnings"))
                low_confidence_examples = _stored_low_confidence_examples(
                    metadata.get("low_confidence_examples"),
                    metadata.get("warnings"),
                )
                positioned_table_words = metadata.get("positioned_table_words") is True
                original_cells = [
                    [TableCell(**cell) for cell in cells]
                    for cells in metadata.get("table_cells", [])
                ]
                stored_source_cells = metadata.get("source_table_cells")
                has_source_table_cells = isinstance(stored_source_cells, list)
                if has_source_table_cells:
                    source_table_cells = [
                        [TableCell(**cell) for cell in cells]
                        for cells in stored_source_cells
                    ]
                else:
                    source_table_cells = [
                        [TableCell(**cell) for cell in cells]
                        for cells in metadata.get("table_cells", [])
                    ]
                original_layouts = [
                    TableLayout(**layout)
                    for layout in metadata.get("table_layouts", [])
                ]
                original_paragraphs = [
                    str(value) for value in metadata.get("paragraphs", [])
                ]
                paragraph_styles = [str(value) for value in metadata.get("paragraph_styles", [])]
                paragraph_layouts = [
                    _deserialize_paragraph_layout(layout)
                    for layout in metadata.get("paragraph_layouts", [])
                ]
                positioned_words = _deserialize_positioned_words(metadata.get("positioned_words", []))
                stored_source_words = metadata.get("source_positioned_words")
                source_positioned_words = (
                    _deserialize_positioned_words(stored_source_words)
                    if isinstance(stored_source_words, list)
                    else list(positioned_words)
                )
                if not has_source_table_cells:
                    source_table_cells = recover_source_table_cells(
                        original_cells,
                        source_positioned_words,
                        positioned_words,
                    )
                source_image_url = metadata.get("source_image_url")
                source_image_file = metadata.get("source_image_file")
                stored_paper_colors = metadata.get("source_paper_colors")
                if isinstance(stored_paper_colors, dict):
                    source_paper_colors = {
                        str(key): str(value)
                        for key, value in stored_paper_colors.items()
                        if isinstance(key, str) and isinstance(value, str)
                    }
                image_width = int(metadata.get("image_width", 0) or 0)
                image_height = int(metadata.get("image_height", 0) or 0)
                if isinstance(source_image_file, str) and source_image_file:
                    candidate = storage.path(Path(source_image_file).name)
                    if is_non_empty_file(candidate):
                        source_image_path = candidate
            except (OSError, TypeError, ValueError):
                original_cells = []
                original_layouts = []
                original_paragraphs = []
                paragraph_styles = []
                paragraph_layouts = []
                positioned_words = []
                source_positioned_words = []
                source_table_cells = []
                source_image_url = None
                low_confidence_examples = []
                source_paper_colors = {}
                warnings = []
                image_width = 0
                image_height = 0

    if source_image_path is None and source_image_file:
        try:
            source_image_temp = storage.path(
                f"{document_id}_source{_source_image_suffix(source_image_file)}"
            )
            storage.download_published(source_image_file, source_image_temp)
            if not is_non_empty_file(source_image_temp):
                raise FileNotFoundError(source_image_file)
            source_image_path = source_image_temp
        except Exception:
            # OSS 过期/被清理时可能抛出供应商自定义异常（例如 NoSuchKey），
            # 不能让它穿透成 500；后面统一返回“原图版式已失效”的 409。
            if source_image_temp is not None:
                source_image_temp.unlink(missing_ok=True)
                source_image_temp = None
            source_image_path = None

    if source_image_path is None:
        # Metadata URLs can contain an untrusted Host header. Keep failed or
        # legacy image references out of server and browser network requests.
        source_image_url = None
    else:
        # Do not propagate a Host-derived URL stored by an earlier request.
        source_image_url = source_image_endpoint(http_request, document_id)

    if source_image_path is not None and (image_width <= 0 or image_height <= 0):
        try:
            with Image.open(source_image_path) as source_image:
                image_width, image_height = source_image.size
        except (OSError, ValueError):
            image_width = image_height = 0

    _validate_positioned_word_bounds(
        request.positioned_words,
        image_width,
        image_height,
    )

    if not source_paper_colors and source_image_path is not None:
        source_paper_colors = sample_source_paper_colors(
            source_image_path,
            OcrResult(
                positioned_words=source_positioned_words,
                table_cells=source_table_cells,
            ),
        )

    # 表格必须依赖原图作为版式底图。拿不到原图时如果继续走原生二维表格
    # 生成，会悄悄丢失合并单元格、标题和空白区域，最终看起来像固定模板。
    if request.tables and source_image_path is None:
        raise HTTPException(
            status_code=409,
            detail="表格原图版式已失效，请重新识别图片后再编辑",
        )

    # 将编辑后的左上角文字同步回原始合并单元格，保留表单结构。
    for table_index, cells in enumerate(original_cells):
        if table_index >= len(request.tables):
            continue
        edited_table = request.tables[table_index]
        for cell in cells:
            if 0 <= cell.row_start < len(edited_table) and 0 <= cell.col_start < len(edited_table[cell.row_start]):
                edited_text = edited_table[cell.row_start][cell.col_start]
                if clean_ocr_text(edited_text) != clean_ocr_text(cell.text):
                    table_cell_edits.append((cell, edited_text))
                cell.text = edited_text
    effective_paragraph_styles = _effective_paragraph_styles(
        original_paragraphs,
        paragraph_styles,
        bool(request.tables),
    )
    edited_positioned_words = _merge_coordinate_words(
        request.positioned_words,
        positioned_words,
        original_cells,
    )
    measured_widths = {
        int(word.source_index): float(word.rendered_width)
        for word in request.positioned_words
        if word.source_index is not None
        and word.rendered_width is not None
        and 0 <= word.source_index < len(positioned_words)
        and abs(word.left - positioned_words[word.source_index].left) <= 1.5
        and abs(word.top - positioned_words[word.source_index].top) <= 1.5
    }
    edited_positioned_words = _fit_coordinate_word_edits(
        positioned_words,
        edited_positioned_words,
        original_cells,
        image_width,
        image_height,
        measured_widths,
    )
    edited_positioned_words = _apply_table_cell_edits(
        edited_positioned_words,
        table_cell_edits,
    )
    edited_positioned_words = _center_coordinate_heading_edits(
        positioned_words,
        edited_positioned_words,
        original_paragraphs,
        effective_paragraph_styles,
        original_cells,
        image_width,
        image_height,
    )
    edited_paragraphs = _sync_paragraphs_from_positioned_words(
        original_paragraphs,
        request.paragraphs,
        positioned_words,
        edited_positioned_words,
    )
    edited_positioned_words = _apply_paragraph_edits(
        edited_positioned_words,
        original_paragraphs,
        edited_paragraphs,
        effective_paragraph_styles,
    )
    edited_tables = (
        _sync_table_text_from_positioned_words(
            request.tables,
            original_cells,
            source_positioned_words,
            edited_positioned_words,
        )
        if positioned_table_words
        else [[list(row) for row in table] for table in request.tables]
    )
    for table_index, cells in enumerate(original_cells):
        if table_index >= len(edited_tables):
            continue
        table = edited_tables[table_index]
        for cell in cells:
            if (
                0 <= cell.row_start < len(table)
                and 0 <= cell.col_start < len(table[cell.row_start])
            ):
                cell.text = table[cell.row_start][cell.col_start]
    source_overlay_needed = _has_positioned_word_changes(
        source_positioned_words,
        edited_positioned_words,
    )
    edited_result = OcrResult(
        image_width=image_width,
        image_height=image_height,
        paragraphs=edited_paragraphs,
        paragraph_styles=effective_paragraph_styles,
        paragraph_layouts=paragraph_layouts,
        positioned_words=edited_positioned_words,
        tables=edited_tables,
        table_layouts=original_layouts,
        table_cells=original_cells,
        low_confidence_examples=low_confidence_examples,
    )
    export_status_warnings = TABLE_PDF_WARNINGS | TABLE_STRUCTURED_DOCX_WARNINGS
    warnings[:] = [warning for warning in warnings if warning not in export_status_warnings]
    has_visual_edits = bool(
        source_image_path
        and (
            edited_result.tables
            or (source_positioned_words and source_overlay_needed)
        )
    )
    try:
        create_docx(
            edited_result,
            docx_path,
            source_image_path,
            original_words=source_positioned_words if has_visual_edits else None,
            original_table_cells=source_table_cells,
            source_positioned_words=source_positioned_words,
            title=document_title,
        )
        if edited_result.tables or _ocr_result_has_content(edited_result):
            structured_docx_path = storage.path(f"{document_id}_structured.docx")
            try:
                create_native_table_docx(
                    edited_result,
                    structured_docx_path,
                    source_image_path,
                    original_table_cells=source_table_cells,
                    title=document_title,
                )
                warnings.append(
                    TABLE_STRUCTURED_DOCX_WARNING
                    if edited_result.tables
                    else TEXT_STRUCTURED_DOCX_WARNING
                )
            except Exception as exc:
                logger.exception(
                    "Edited structured Word generation failed; preserving the "
                    "original-layout document (document_id=%s, error_type=%s)",
                    document_id,
                    type(exc).__name__,
                )
                try:
                    structured_docx_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Failed to remove partial edited structured Word file %s",
                        structured_docx_path,
                        exc_info=True,
                    )
                structured_docx_path = None
                warnings.append(
                    TABLE_STRUCTURED_DOCX_UNAVAILABLE_WARNING
                    if edited_result.tables
                    else TEXT_STRUCTURED_DOCX_UNAVAILABLE_WARNING
                )
        if has_visual_edits and (not edited_result.tables or source_image_path is not None):
            rendered_pdf_image_path = storage.path(f"{document_id}_edited.png")
            try:
                render_positioned_image(
                    edited_result,
                    source_image_path,
                    rendered_pdf_image_path,
                    original_words=source_positioned_words,
                )
            except Exception as exc:
                rendered_pdf_image_path.unlink(missing_ok=True)
                rendered_pdf_image_path = None
                logger.warning(
                    "Edited scan rendering failed for document %s (error_type=%s)",
                    document_id,
                    type(exc).__name__,
                )
        if edited_result.tables:
            try:
                pdf_path, used_image_fallback = convert_docx_to_pdf(
                    docx_path,
                    storage.root,
                    fallback_image_path=rendered_pdf_image_path,
                    prefer_image=rendered_pdf_image_path is not None,
                )
            except Exception as exc:
                logger.warning(
                    "Edited visual PDF conversion failed for document %s "
                    "(error_type=%s)",
                    document_id,
                    type(exc).__name__,
                )
            if structured_docx_path is not None:
                try:
                    structured_pdf_path, structured_pdf_used_image = convert_docx_to_pdf(
                        structured_docx_path,
                        storage.root,
                    )
                    if structured_pdf_used_image:
                        structured_pdf_path = None
                except Exception as exc:
                    logger.warning(
                        "Edited searchable table PDF conversion failed for document %s "
                        "(error_type=%s)",
                        document_id,
                        type(exc).__name__,
                    )
            table_pdf_warnings = []
            if pdf_path is not None:
                table_pdf_warnings.append(TABLE_IMAGE_PDF_WARNING)
            else:
                table_pdf_warnings.append(TABLE_PDF_UNAVAILABLE_WARNING)
            if structured_pdf_path is not None:
                table_pdf_warnings.append(TABLE_TEXT_PDF_WARNING)
            else:
                table_pdf_warnings.append(TABLE_TEXT_PDF_UNAVAILABLE_WARNING)
            _set_table_pdf_warning(warnings, *table_pdf_warnings)
        else:
            pdf_conversion_failed = False
            try:
                pdf_path, used_image_fallback = convert_docx_to_pdf(
                    docx_path,
                    storage.root,
                    fallback_image_path=rendered_pdf_image_path,
                    prefer_image=bool(rendered_pdf_image_path),
                )
            except Exception as exc:
                logger.warning(
                    "Edited document PDF conversion failed for document %s "
                    "(error_type=%s)",
                    document_id,
                    type(exc).__name__,
                )
                pdf_conversion_failed = True
                warnings.append("PDF 转换失败，请稍后重试")
            if pdf_path is None and not pdf_conversion_failed:
                warnings.append("PDF 暂未生成，请检查服务器文档转换组件")
            elif used_image_fallback:
                if rendered_pdf_image_path is not None:
                    warnings.append("已按原图版式生成图片型 PDF")
                else:
                    warnings.append("服务器未安装 LibreOffice，已生成图片型 PDF")
            if structured_docx_path is not None:
                try:
                    structured_pdf_path, structured_pdf_used_image = convert_docx_to_pdf(
                        structured_docx_path,
                        storage.root,
                    )
                    if structured_pdf_used_image:
                        structured_pdf_path = None
                except Exception as exc:
                    logger.warning(
                        "Edited copyable OCR PDF conversion failed for document %s "
                        "(error_type=%s)",
                        document_id,
                        type(exc).__name__,
                    )
                _set_table_pdf_warning(
                    warnings,
                    TABLE_TEXT_PDF_WARNING
                    if structured_pdf_path is not None
                    else TABLE_TEXT_PDF_UNAVAILABLE_WARNING,
                )

        published_names.append(docx_path.name)
        docx_url = storage.publish(docx_path)
        if structured_docx_path is not None:
            published_names.append(structured_docx_path.name)
        structured_docx_url = (
            storage.publish(structured_docx_path)
            if structured_docx_path is not None
            else None
        )
        if structured_pdf_path:
            published_names.append(structured_pdf_path.name)
        structured_pdf_url = storage.publish(structured_pdf_path) if structured_pdf_path else None
        if pdf_path:
            published_names.append(pdf_path.name)
        pdf_url = storage.publish(pdf_path) if pdf_path else None
        output_metadata_path = metadata_path(document_id)
        warnings = _stored_document_warnings(warnings)
        serialized_paragraph_layouts = [
            _serialize_paragraph_layout(layout)
            for layout in paragraph_layouts
        ]
        serialized_positioned_words = [
            _serialize_positioned_word(word) for word in edited_positioned_words
        ]
        serialized_source_positioned_words = [
            _serialize_positioned_word(word) for word in source_positioned_words
        ]
        serialized_table_layouts = [asdict(layout) for layout in original_layouts]
        serialized_table_cells = [
            [asdict(cell) for cell in cells]
            for cells in original_cells
        ]
        serialized_source_table_cells = [
            [asdict(cell) for cell in cells]
            for cells in source_table_cells
        ]
        output_metadata_path.write_text(
            json.dumps(
                {
                    "title": document_title,
                    "image_width": image_width,
                    "image_height": image_height,
                    "paragraphs": edited_paragraphs,
                    "paragraph_styles": effective_paragraph_styles,
                    "paragraph_layouts": serialized_paragraph_layouts,
                    "positioned_words": serialized_positioned_words,
                    "source_positioned_words": serialized_source_positioned_words,
                    "plain_text_layout_version": (
                        PLAIN_TEXT_LAYOUT_VERSION
                        if not edited_tables and not edited_positioned_words
                        else None
                    ),
                    "positioned_table_words": positioned_table_words,
                    "low_confidence_examples": low_confidence_examples,
                    "source_image_url": source_image_url,
                    "source_paper_colors": source_paper_colors,
                    # When the source lives in OSS, source_image_path is only a
                    # temporary local download. Keep the durable object name in
                    # child history metadata so it remains reopenable.
                    "source_image_file": source_image_file,
                    "docx_file": docx_path.name,
                    "visual_docx_layout_version": (
                        VISUAL_DOCX_LAYOUT_VERSION
                        if has_visual_edits
                        else None
                    ),
                    "structured_docx_file": (
                        structured_docx_path.name
                        if structured_docx_path is not None
                        else None
                    ),
                    "structured_docx_layout_version": (
                        STRUCTURED_DOCX_LAYOUT_VERSION if structured_docx_path is not None else None
                    ),
                    "structured_pdf_layout_version": (
                        TABLE_PDF_LAYOUT_VERSION
                        if structured_pdf_path is not None
                        else None
                    ),
                    "visual_pdf_layout_version": (
                        VISUAL_PDF_LAYOUT_VERSION
                        if (edited_result.tables or rendered_pdf_image_path)
                        and pdf_path is not None
                        else None
                    ),
                    "pdf_file": pdf_path.name if pdf_path else None,
                    "structured_pdf_file": (
                        structured_pdf_path.name if structured_pdf_path else None
                    ),
                    "warnings": warnings,
                    "tables": edited_tables,
                    "table_layouts": serialized_table_layouts,
                    "table_cells": serialized_table_cells,
                    "source_table_cells": serialized_source_table_cells,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        cleanup_failed_export()
        logger.error(
            "Edited document publication failed (document_id=%s, error_type=%s)",
            document_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="编辑后的文件暂时无法保存，请稍后重试",
        ) from exc

    if source_image_temp:
        source_image_temp.unlink(missing_ok=True)
    if rendered_pdf_image_path:
        rendered_pdf_image_path.unlink(missing_ok=True)

    return DocumentResponse(
        id=document_id,
        title=document_title,
        has_tables=bool(edited_tables),
        docx_url=document_download_endpoint(document_id, "docx"),
        docx_preserves_source_layout=has_visual_edits,
        structured_docx_url=(
            document_download_endpoint(document_id, "structured-docx")
            if structured_docx_url
            else None
        ),
        pdf_url=(
            document_download_endpoint(document_id, "pdf")
            if pdf_url
            else None
        ),
        pdf_preserves_source_layout=bool(
            pdf_url and (edited_result.tables or rendered_pdf_image_path)
        ),
        structured_pdf_url=(
            document_download_endpoint(document_id, "structured-pdf")
            if structured_pdf_url
            else None
        ),
        warnings=warnings,
        paragraphs=edited_paragraphs,
        paragraph_styles=effective_paragraph_styles,
        paragraph_layouts=paragraph_layouts,
        tables=edited_tables,
        table_layouts=serialized_table_layouts,
        table_cells=serialized_table_cells,
        image_width=edited_result.image_width,
        image_height=edited_result.image_height,
        positioned_words=serialized_positioned_words,
        editor_html=render_editor_html(
            edited_result,
            source_image_url,
            source_image_endpoint(http_request, document_id, original=True)
            if source_image_url
            else None,
            source_positioned_words,
            source_table_cells,
            source_paper_colors,
            positioned_table_words=positioned_table_words,
        ),
        source_image_url=source_image_url,
    )
