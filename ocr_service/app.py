from __future__ import annotations

import asyncio
import hmac
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any

import pytesseract
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from pytesseract import Output
from starlette.concurrency import run_in_threadpool


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


MAX_UPLOAD_BYTES = _positive_int("OCR_MAX_UPLOAD_MB", 20) * 1024 * 1024
MAX_IMAGE_PIXELS = _positive_int("OCR_MAX_IMAGE_PIXELS", 50_000_000)
OCR_TIMEOUT_SECONDS = _positive_int("OCR_TIMEOUT_SECONDS", 75)
OCR_LANGUAGE = os.getenv("OCR_LANG", "chi_sim+eng").strip() or "chi_sim+eng"
OCR_LANGUAGE_PARTS = [part.strip() for part in OCR_LANGUAGE.split("+") if part.strip()]
OCR_CONCURRENCY = _positive_int("OCR_CONCURRENCY", 1)
_ocr_slots = asyncio.Semaphore(OCR_CONCURRENCY)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not os.getenv("OCR_API_KEY", "").strip():
        raise RuntimeError("OCR_API_KEY must be configured before starting the OCR service")
    if not OCR_LANGUAGE_PARTS or any(
        not all(character.isalnum() or character in "_.-" for character in language)
        for language in OCR_LANGUAGE_PARTS
    ):
        raise RuntimeError("OCR_LANG contains an invalid language code")
    try:
        pytesseract.get_tesseract_version()
        installed_languages = set(pytesseract.get_languages(config=""))
    except (pytesseract.TesseractNotFoundError, RuntimeError) as exc:
        raise RuntimeError("Tesseract is missing or could not be started") from exc
    missing_languages = sorted(set(OCR_LANGUAGE_PARTS) - installed_languages)
    if missing_languages:
        raise RuntimeError(
            "Missing Tesseract language data: " + ", ".join(missing_languages)
        )
    yield


app = FastAPI(title="DocuCraft OCR Service", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "engine": "tesseract", "languages": OCR_LANGUAGE}


def _join_line(tokens: list[str]) -> str:
    result = ""
    previous = ""
    for token in tokens:
        if not token:
            continue
        needs_space = bool(
            previous
            and token[:1].isascii()
            and token[:1].isalnum()
            and (
                (previous[-1:].isascii() and previous[-1:].isalnum())
                or previous[-1:] in {",", ".", ";", ":", "!", "?"}
            )
        )
        if needs_space:
            result += " "
        result += token
        previous = token
    return result.strip()


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

    try:
        data = pytesseract.image_to_data(
            image,
            lang=OCR_LANGUAGE,
            config="--oem 1 --psm 6",
            output_type=Output.DICT,
            timeout=OCR_TIMEOUT_SECONDS,
        )
    except (pytesseract.TesseractError, RuntimeError) as exc:
        raise RuntimeError("Tesseract OCR failed") from exc

    paragraph_lines: OrderedDict[tuple[int, int, int], OrderedDict[int, list[str]]] = (
        OrderedDict()
    )
    positioned_words: list[dict[str, Any]] = []
    confidences: list[float] = []
    low_confidence_examples: list[str] = []

    for index, raw_text in enumerate(data.get("text", [])):
        text = " ".join(str(raw_text).split())
        if not text:
            continue
        try:
            confidence = float(data["conf"][index])
            page = int(data["page_num"][index])
            block = int(data["block_num"][index])
            paragraph = int(data["par_num"][index])
            line = int(data["line_num"][index])
            left = int(data["left"][index])
            top = int(data["top"][index])
            word_width = int(data["width"][index])
            word_height = int(data["height"][index])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if confidence < 0:
            continue

        confidences.append(confidence)
        if confidence < 40 and len(low_confidence_examples) < 5:
            low_confidence_examples.append(text)
        paragraph_lines.setdefault((page, block, paragraph), OrderedDict()).setdefault(
            line, []
        ).append(text)
        if word_width > 0 and word_height > 0:
            positioned_words.append(
                {
                    "text": text,
                    "location": {
                        "left": left,
                        "top": top,
                        "width": word_width,
                        "height": word_height,
                    },
                    "confidence": confidence,
                }
            )

    paragraphs = [
        "\n".join(_join_line(tokens) for tokens in lines.values()).strip()
        for lines in paragraph_lines.values()
    ]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    average_confidence = (
        sum(confidences) / len(confidences) if confidences else 0.0
    )
    return {
        "text": "\n\n".join(paragraphs),
        "paragraphs": paragraphs,
        "positioned_words": positioned_words,
        "image_width": width,
        "image_height": height,
        "low_text_confidence": bool(confidences) and average_confidence < 60,
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
