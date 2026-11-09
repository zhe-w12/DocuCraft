from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class LineGuides:
    """Horizontal and vertical table-line centres in source-image pixels."""

    x: list[float]
    y: list[float]


def detect_table_line_guides(
    image_path: Path,
    bounds: tuple[float, float, float, float],
) -> LineGuides:
    """Detect long table rules while ignoring ordinary text strokes.

    OCR remains responsible for text and merged-cell semantics.  These guides
    provide the actual geometry from the pixels, so Word and the mobile editor
    do not have to guess column widths from cell text.
    """

    try:
        import cv2
        import numpy as np
    except ImportError:
        return _detect_table_line_guides_pillow(image_path, bounds)

    encoded = np.fromfile(str(image_path), dtype=np.uint8)
    gray = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
    if gray is None or gray.size == 0:
        return LineGuides([], [])

    image_height, image_width = gray.shape[:2]
    left, top, right, bottom = bounds
    pad_x = max(int((right - left) * 0.02), 3)
    pad_y = max(int((bottom - top) * 0.02), 3)
    crop_left = max(int(left) - pad_x, 0)
    crop_top = max(int(top) - pad_y, 0)
    crop_right = min(int(right) + pad_x + 1, image_width)
    crop_bottom = min(int(bottom) + pad_y + 1, image_height)
    if crop_right - crop_left < 20 or crop_bottom - crop_top < 20:
        return LineGuides([], [])

    crop = gray[crop_top:crop_bottom, crop_left:crop_right]
    crop_height, crop_width = crop.shape[:2]
    block_size = max(15, min(51, (min(crop_width, crop_height) // 24) | 1))
    binary = cv2.adaptiveThreshold(
        crop,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block_size,
        11,
    )

    horizontal_length = max(12, crop_width // 35)
    vertical_length = max(12, crop_height // 45)
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
    horizontal = cv2.morphologyEx(
        horizontal,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, crop_width // 180), 1)),
    )
    vertical = cv2.morphologyEx(
        vertical,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, crop_height // 220))),
    )

    x_scores = np.count_nonzero(vertical, axis=0) / max(crop_height, 1)
    y_scores = np.count_nonzero(horizontal, axis=1) / max(crop_width, 1)
    x_local = _cluster_projection(x_scores, minimum_score=0.018, maximum_thickness=max(9, crop_width // 45))
    # A real horizontal rule normally covers a substantial part of the table;
    # the higher projection threshold rejects underlines and Chinese strokes.
    y_local = _cluster_projection(y_scores, minimum_score=0.15, maximum_thickness=max(9, crop_height // 55))
    primary = LineGuides(
        [float(value + crop_left) for value in x_local],
        [float(value + crop_top) for value in y_local],
    )
    fallback = _detect_table_line_guides_pillow(image_path, bounds)
    return LineGuides(
        _prefer_axis(primary.x, fallback.x),
        _prefer_axis(primary.y, fallback.y),
    )


def _detect_table_line_guides_pillow(
    image_path: Path,
    bounds: tuple[float, float, float, float],
) -> LineGuides:
    """Dependency-light line detector used before OpenCV is installed.

    A table rule is distinguished from text by its longest continuous dark
    run: Chinese glyphs are short disconnected strokes, while a cell rule
    crosses a substantial part of the crop.  This keeps local development and
    recovery jobs from silently reverting to OCR-only geometry.
    """

    try:
        from PIL import Image
    except ImportError:
        return LineGuides([], [])

    try:
        with Image.open(image_path) as source:
            gray = source.convert("L")
            image_width, image_height = gray.size
            left, top, right, bottom = bounds
            pad_x = max(int((right - left) * 0.02), 3)
            pad_y = max(int((bottom - top) * 0.02), 3)
            crop_left = max(int(left) - pad_x, 0)
            crop_top = max(int(top) - pad_y, 0)
            crop_right = min(int(right) + pad_x + 1, image_width)
            crop_bottom = min(int(bottom) + pad_y + 1, image_height)
            if crop_right - crop_left < 20 or crop_bottom - crop_top < 20:
                return LineGuides([], [])
            crop = gray.crop((crop_left, crop_top, crop_right, crop_bottom))
            width, height = crop.size
            pixels = crop.load()
            row_scores = [
                _longest_dark_run(
                    (pixels[x, y] < 220 for x in range(width)),
                    width,
                )
                / max(width, 1)
                for y in range(height)
            ]
            column_scores = [
                _longest_dark_run(
                    (pixels[x, y] < 220 for y in range(height)),
                    height,
                )
                / max(height, 1)
                for x in range(width)
            ]
    except (OSError, ValueError):
        return LineGuides([], [])

    x_local = _cluster_projection(
        column_scores,
        minimum_score=0.30,
        maximum_thickness=max(9, width // 45),
    )
    y_local = _cluster_projection(
        row_scores,
        minimum_score=0.30,
        maximum_thickness=max(9, height // 55),
    )
    return LineGuides(
        [float(value + crop_left) for value in x_local],
        [float(value + crop_top) for value in y_local],
    )


def _longest_dark_run(values, length: int, gap_tolerance: int = 4) -> int:
    """Return the longest dark run while bridging tiny anti-aliasing gaps."""

    longest = 0
    current = 0
    gap = 0
    for dark in values:
        if dark:
            current += gap + 1
            gap = 0
        elif current:
            gap += 1
            if gap > gap_tolerance:
                longest = max(longest, current)
                current = 0
                gap = 0
    return max(longest, current)


def _prefer_axis(primary: list[float], fallback: list[float]) -> list[float]:
    """Use the pixel fallback when the primary detector missed real rules."""

    if len(primary) < 2:
        return fallback or primary
    if len(fallback) > len(primary) + 1:
        return fallback
    return primary


def _cluster_projection(scores, minimum_score: float, maximum_thickness: int) -> list[float]:
    indices = [int(index) for index, value in enumerate(scores) if value >= minimum_score]
    if not indices:
        return []

    groups: list[list[int]] = [[indices[0]]]
    for index in indices[1:]:
        if index <= groups[-1][-1] + 1:
            groups[-1].append(index)
        else:
            groups.append([index])

    centres: list[float] = []
    for group in groups:
        if len(group) > maximum_thickness:
            continue
        weights = [float(scores[index]) for index in group]
        total = sum(weights)
        centre = sum(index * weight for index, weight in zip(group, weights)) / total if total else sum(group) / len(group)
        centres.append(float(centre))
    return centres
