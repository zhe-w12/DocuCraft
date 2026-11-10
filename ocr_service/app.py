from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import Iterable
from contextlib import asynccontextmanager
from io import BytesIO
from math import isfinite
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from PIL import Image, ImageDraw, UnidentifiedImageError
from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR
from starlette.concurrency import run_in_threadpool


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


MAX_UPLOAD_BYTES = _positive_int("OCR_MAX_UPLOAD_MB", 20) * 1024 * 1024
MAX_IMAGE_PIXELS = _positive_int("OCR_MAX_IMAGE_PIXELS", 50_000_000)
OCR_CONCURRENCY = _positive_int("OCR_CONCURRENCY", 1)
_ocr_slots = asyncio.Semaphore(OCR_CONCURRENCY)
_ocr_engine: RapidOCR | None = None


def _create_ocr_engine() -> RapidOCR:
    return RapidOCR(
        params={
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.model_type": ModelType.SMALL,
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.model_type": ModelType.SMALL,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
        }
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _ocr_engine
    if not os.getenv("OCR_API_KEY", "").strip():
        raise RuntimeError("OCR_API_KEY must be configured before starting the OCR service")
    try:
        _ocr_engine = _create_ocr_engine()
        # Load and exercise the bundled models before marking the service ready.
        warmup_image = Image.new("RGB", (160, 48), "white")
        ImageDraw.Draw(warmup_image).text((12, 12), "OCR", fill="black")
        await run_in_threadpool(_ocr_engine, warmup_image)
    except Exception as exc:
        _ocr_engine = None
        raise RuntimeError("RapidOCR models could not be initialized") from exc
    yield
    _ocr_engine = None


app = FastAPI(title="DocuCraft OCR Service", version="2.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    if _ocr_engine is None:
        raise HTTPException(status_code=503, detail="OCR engine is not ready")
    return {
        "status": "ok",
        "engine": "rapidocr",
        "model": "PP-OCRv6-small",
        "languages": "chinese_simplified+english",
    }


def _flatten_numbers(value: Any) -> Iterable[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten_numbers(item)
        return
    yield float(value)


def _line_detections(result: Any, width: int, height: int) -> list[dict[str, Any]]:
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None or texts is None:
        return []

    detections: list[dict[str, Any]] = []
    for index, (raw_text, raw_box) in enumerate(zip(texts, boxes)):
        text = " ".join(str(raw_text or "").split())
        if not text:
            continue
        try:
            coordinates = list(_flatten_numbers(raw_box))
            confidence = float(scores[index]) if scores is not None else 0.0
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
        if not isfinite(confidence) or len(coordinates) < 4:
            continue
        if len(coordinates) == 4:
            x_values = (coordinates[0], coordinates[2])
            y_values = (coordinates[1], coordinates[3])
        else:
            x_values = coordinates[0::2]
            y_values = coordinates[1::2]
        if not x_values or not y_values or not all(
            isfinite(value) for value in (*x_values, *y_values)
        ):
            continue

        left = max(0.0, min(x_values))
        top = max(0.0, min(y_values))
        right = min(float(width), max(x_values))
        bottom = min(float(height), max(y_values))
        if right <= left or bottom <= top:
            continue
        detections.append(
            {
                "text": text,
                "left": int(round(left)),
                "top": int(round(top)),
                "width": max(int(round(right - left)), 1),
                "height": max(int(round(bottom - top)), 1),
                "confidence": max(0.0, min(confidence, 1.0)) * 100,
            }
        )

    return sorted(detections, key=lambda item: (item["top"], item["left"]))


def _recognize(image_bytes: bytes) -> dict[str, Any]:
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            width, height = source.size
            if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail="Image dimensions exceed the OCR limit")
            source.load()
            image = source.convert("RGB")
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=400, detail="The uploaded file is not a valid image") from exc

    if _ocr_engine is None:
        raise RuntimeError("RapidOCR is not initialized")
    try:
        result = _ocr_engine(image)
    except Exception as exc:
        raise RuntimeError("RapidOCR inference failed") from exc

    detections = _line_detections(result, width, height)
    # Keep one recognized line per paragraph here. DocuCraft's backend uses
    # these line strings together with the boxes below to restore prose
    # paragraphs and identify headings from the original page geometry.
    paragraphs = [item["text"] for item in detections]
    confidences = [item["confidence"] for item in detections]
    average_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    low_confidence_examples = [
        item["text"] for item in detections if item["confidence"] < 60
    ][:5]
    positioned_words = [
        {
            "text": item["text"],
            "location": {
                "left": item["left"],
                "top": item["top"],
                "width": item["width"],
                "height": item["height"],
            },
            "confidence": item["confidence"],
        }
        for item in detections
    ]
    return {
        "text": "\n".join(paragraphs),
        "paragraphs": paragraphs,
        "positioned_words": positioned_words,
        "image_width": width,
        "image_height": height,
        "low_text_confidence": bool(confidences) and average_confidence < 70,
        "low_confidence_examples": low_confidence_examples,
    }


@app.post("/ocr")
async def recognize_image(
    image: UploadFile = File(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    expected_key = os.getenv("OCR_API_KEY", "").strip()
    scheme, separator, supplied_key = (authorization or "").partition(" ")
    if (
        not expected_key
        or not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(supplied_key.strip(), expected_key)
    ):
        raise HTTPException(status_code=401, detail="Invalid OCR service credentials")

    image_bytes = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Uploaded image exceeds the OCR size limit")
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Uploaded image is empty")

    async with _ocr_slots:
        try:
            return await run_in_threadpool(_recognize, image_bytes)
        except HTTPException:
            raise
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail="OCR engine failed to process the image") from exc
