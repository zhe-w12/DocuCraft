# OCR Benchmarking

`backend/scripts/benchmark_ocr.py` compares OCR providers against the same local images and human-corrected transcriptions. It reads the corpus locally and does not upload images. Reports omit recognized text by default. Keep benchmark images, annotations, and reports outside the repository so personal documents are not committed.

## Prepare annotations

Create a UTF-8 JSONL file with one image per line. Image paths may be relative to the manifest directory or absolute:

```jsonl
{"id":"clear-text-01","image":"images/clear-text-01.png","expected_text":"Human-corrected body text goes here"}
{"id":"table-01","image":"images/table-01.png","expected_tables":[[["Name","Age"],["Alex","18"]]]}
```

- `expected_text` is used to calculate character error rate (CER). The script compares recognized paragraphs, falling back to table text or positioned words when the provider returns no paragraphs. Unicode is normalized to NFC and whitespace is ignored; punctuation is preserved.
- `expected_tables` is used to calculate table-cell accuracy. The nested shape is “tables → rows → cells”. Cells are compared exactly at each row and column; missing or extra cells count as errors.
- A record may include both fields. Review annotations by hand rather than using OCR output as ground truth.

## Run

Activate the backend virtual environment and run from `backend/`. Before evaluating your service, configure `OCR_ENDPOINT` and the optional API key as described in the [self-hosted OCR guide](self-hosted-ocr.en.md):

```sh
python scripts/benchmark_ocr.py \
  --provider generic_http \
  --manifest /path/to/ocr-corpus/manifest.jsonl \
  --output /path/to/ocr-corpus/self-hosted-results.json
```

`--provider` accepts `mock` and `generic_http`. For `generic_http`, configure `OCR_ENDPOINT` and the optional `OCR_API_KEY` in the current process environment. The report contains per-image CER, table-cell accuracy, and latency, plus micro-average CER, accuracy, and mean latency. Use `--limit 10` to check a small subset first.

Recognized text is omitted from reports by default. Use `--include-recognized-text` only when the report location is appropriate and the sample documents contain no sensitive information.

## Interpret results

Lower CER and higher table-cell accuracy are better. Maintain separate groups for clean print, low-contrast or skewed images, and complex tables; one aggregate score can hide regressions on a specific image type. Use the same samples, image versions, and language-data settings for each comparison. Review OCR output manually; benchmark scores do not guarantee accuracy on production scans.
