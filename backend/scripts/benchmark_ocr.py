#!/usr/bin/env python3
"""Compare an OCR provider against a local JSONL transcription manifest.

The corpus stays wherever the operator keeps it; this script never uploads
images or writes recognized text unless --include-recognized-text is supplied.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value))
    return "".join(text.split())


def _character_errors(reference: str, hypothesis: str) -> int:
    """Return Levenshtein distance using O(min(n, m)) memory."""
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row, expected in enumerate(reference, start=1):
        current = [row]
        for column, actual in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected != actual),
                )
            )
        previous = current
    return previous[-1]


def _result_text(result: Any) -> str:
    paragraphs = [str(text) for text in result.paragraphs if str(text).strip()]
    if paragraphs:
        return "\n".join(paragraphs)
    table_lines = [
        "\t".join(str(cell) for cell in row)
        for table in result.tables
        for row in table
    ]
    if table_lines:
        return "\n".join(table_lines)
    return "\n".join(word.text for word in result.positioned_words if word.text)


def _table_cell_counts(expected: list[Any], actual: list[Any]) -> tuple[int, int]:
    """Compare table cells by table/row/column; shape differences count as errors."""
    expected_cells = {
        (table_index, row_index, column_index): _normalize(value)
        for table_index, table in enumerate(expected)
        if isinstance(table, list)
        for row_index, row in enumerate(table)
        if isinstance(row, list)
        for column_index, value in enumerate(row)
    }
    actual_cells = {
        (table_index, row_index, column_index): _normalize(value)
        for table_index, table in enumerate(actual)
        if isinstance(table, list)
        for row_index, row in enumerate(table)
        if isinstance(row, list)
        for column_index, value in enumerate(row)
    }
    positions = expected_cells.keys() | actual_cells.keys()
    matches = sum(
        expected_cells.get(position) == actual_cells.get(position)
        for position in positions
    )
    return matches, len(positions)


def _is_table_annotation(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(table, list)
        and all(
            isinstance(row, list) and all(isinstance(cell, str) for cell in row)
            for row in table
        )
        for table in value
    )


def _read_manifest(path: Path, limit: int | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as manifest:
        for line_number, line in enumerate(manifest, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: each line must be a JSON object")
            if not isinstance(record.get("image"), str) or not record["image"].strip():
                raise ValueError(f"{path}:{line_number}: 'image' must be a non-empty path")
            if "expected_text" in record and not isinstance(
                record["expected_text"], str
            ):
                raise ValueError(f"{path}:{line_number}: 'expected_text' must be a string")
            if "expected_tables" in record and not _is_table_annotation(
                record["expected_tables"]
            ):
                raise ValueError(
                    f"{path}:{line_number}: 'expected_tables' must be tables → rows → string cells"
                )
            if "expected_text" not in record and "expected_tables" not in record:
                raise ValueError(
                    f"{path}:{line_number}: add 'expected_text' and/or 'expected_tables'"
                )
            records.append(record)
            if limit is not None and len(records) >= limit:
                break
    if not records:
        raise ValueError(f"No benchmark records found in {path}")
    return records


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.provider:
        os.environ["OCR_PROVIDER"] = args.provider

    from app.services.ocr import get_ocr_provider

    get_ocr_provider.cache_clear()
    provider = get_ocr_provider()
    records = _read_manifest(args.manifest, args.limit)
    output_records: list[dict[str, Any]] = []
    total_character_errors = 0
    total_reference_characters = 0
    total_table_matches = 0
    total_table_cells = 0
    total_seconds = 0.0
    failures = 0

    for index, record in enumerate(records, start=1):
        image_path = Path(record["image"])
        if not image_path.is_absolute():
            image_path = args.manifest.parent / image_path
        item: dict[str, Any] = {
            "id": str(record.get("id") or f"sample-{index:04d}"),
        }
        started = time.perf_counter()
        try:
            result = await provider.recognize(image_path)
        except Exception as exc:  # keep processing the rest of a benchmark corpus
            elapsed = time.perf_counter() - started
            failures += 1
            total_seconds += elapsed
            item.update(
                status="error",
                error=type(exc).__name__,
                latency_seconds=round(elapsed, 3),
            )
            output_records.append(item)
            print(f"[{index}/{len(records)}] {item['id']}: ERROR ({type(exc).__name__})")
            continue

        elapsed = time.perf_counter() - started
        total_seconds += elapsed
        item.update(status="ok", latency_seconds=round(elapsed, 3))
        if "expected_text" in record:
            reference = _normalize(record["expected_text"])
            hypothesis = _normalize(_result_text(result))
            errors = _character_errors(reference, hypothesis)
            total_character_errors += errors
            total_reference_characters += len(reference)
            item.update(
                character_errors=errors,
                reference_characters=len(reference),
                cer=(
                    round(errors / len(reference), 6)
                    if reference
                    else (0.0 if not hypothesis else 1.0)
                ),
            )
        if isinstance(record.get("expected_tables"), list):
            matches, cells = _table_cell_counts(record["expected_tables"], result.tables)
            total_table_matches += matches
            total_table_cells += cells
            item.update(
                table_cells_matched=matches,
                table_cells_total=cells,
                table_cell_accuracy=round(matches / cells, 6) if cells else None,
            )
        if args.include_recognized_text:
            item["recognized_text"] = _result_text(result)
        output_records.append(item)
        score = f"CER={item['cer']:.4f}" if "cer" in item else ""
        table_score = (
            f" table={item['table_cell_accuracy']:.4f}"
            if item.get("table_cell_accuracy") is not None
            else ""
        )
        print(f"[{index}/{len(records)}] {item['id']}: {score}{table_score} {elapsed:.2f}s")

    return {
        "provider": os.getenv("OCR_PROVIDER", "mock").strip().lower(),
        "records": len(records),
        "successful": len(records) - failures,
        "failed": failures,
        "character_errors": total_character_errors,
        "reference_characters": total_reference_characters,
        "micro_cer": (
            total_character_errors / total_reference_characters
            if total_reference_characters
            else None
        ),
        "table_cells_matched": total_table_matches,
        "table_cells_total": total_table_cells,
        "table_cell_accuracy": (
            total_table_matches / total_table_cells if total_table_cells else None
        ),
        "mean_latency_seconds": total_seconds / len(records),
        "items": output_records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", required=True, type=Path, help="JSONL transcription manifest"
    )
    parser.add_argument(
        "--provider",
        choices=("mock", "generic_http"),
        help="OCR provider (defaults to OCR_PROVIDER or mock)",
    )
    parser.add_argument(
        "--output", type=Path, help="write aggregate and per-image scores as JSON"
    )
    parser.add_argument("--limit", type=int, help="process at most this many records")
    parser.add_argument(
        "--include-recognized-text",
        action="store_true",
        help="include OCR output in JSON (may contain personal data)",
    )
    args = parser.parse_args()
    args.manifest = args.manifest.resolve()
    if not args.manifest.is_file():
        parser.error(f"manifest not found: {args.manifest}")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    try:
        report = asyncio.run(_run(args))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    print(
        "Summary: "
        f"{report['successful']}/{report['records']} succeeded, "
        f"micro CER={report['micro_cer'] if report['micro_cer'] is not None else 'n/a'}, "
        "table cell accuracy="
        f"{report['table_cell_accuracy'] if report['table_cell_accuracy'] is not None else 'n/a'}"
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report_text + "\n", encoding="utf-8")
        print(f"Report written to {args.output}")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
