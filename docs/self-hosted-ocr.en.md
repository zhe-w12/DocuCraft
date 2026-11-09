# Connect a Self-Hosted OCR Service

Production uses the DocuCraft backend's `generic_http` adapter to call your own OCR service. Scans are not sent to Baidu or another third-party OCR service, and recognition does not fall back to Tesseract on the backend machine. Local development can use `mock`.

## Configure the backend

Set the service endpoint and optional API key in the deployment environment file:

```dotenv
OCR_ENDPOINT=http://127.0.0.1:9000/ocr
OCR_API_KEY=replace-with-your-service-key
```

The production Compose file fixes `OCR_PROVIDER=generic_http` and refuses to start without `OCR_ENDPOINT`. If the OCR service runs on another machine, replace `127.0.0.1` with its private DNS name or address. Use HTTPS for traffic between machines.

Local development defaults to `OCR_PROVIDER=mock`. To connect to a real service locally, set these variables before starting the backend:

```powershell
$env:OCR_PROVIDER = "generic_http"
$env:OCR_ENDPOINT = "https://ocr.example.com/recognize"
$env:OCR_API_KEY = "replace-with-your-service-key"
Set-Location backend
python run.py
```

## What the backend sends | HTTP request

The backend sends a `POST multipart/form-data` request to `OCR_ENDPOINT`. The image field is always named `image`; its MIME type is set to PNG or JPEG based on the file format. If `OCR_API_KEY` is configured, the request includes `Authorization: Bearer <key>`. The current HTTP timeout is 90 seconds. Limit request size and concurrency in your OCR service so a single client cannot consume unlimited resources.

To troubleshoot from the backend host, send a sample image that you are authorized to use:

```sh
curl -X POST "https://ocr.example.com/recognize" \
  -H "Authorization: Bearer $OCR_API_KEY" \
  -F "image=@/path/to/scan.png"
```

Do not put real credentials in public documentation, shell history, or screenshots. Omit the `Authorization` header if your service does not require authentication.

## What the OCR service returns | HTTP response

Return a successful HTTP status and a UTF-8 JSON object. The smallest supported response is a list of paragraphs:

```json
{
  "paragraphs": ["First recognized paragraph", "Second recognized paragraph"]
}
```

A single `text` string is also accepted. Text-only results can be exported as editable text, but cannot preserve source positions or table boundaries. For positioned editing and table recovery, return coordinates and table structure as well:

```json
{
  "paragraphs": ["Application form"],
  "positioned_words": [
    {
      "text": "Application form",
      "location": {"left": 120, "top": 48, "width": 160, "height": 36},
      "confidence": 98.5
    }
  ],
  "tables": [[["Name", "Age"], ["Alex", "18"]]],
  "table_layouts": [
    {"column_widths": [180, 90], "row_heights": [36, 34], "source_width": 600}
  ],
  "table_cells": [[
    {"row_start": 0, "row_end": 1, "col_start": 0, "col_end": 1,
     "text": "Name", "location": {"left": 40, "top": 120, "width": 180, "height": 36}},
    {"row_start": 0, "row_end": 1, "col_start": 1, "col_end": 2,
     "text": "Age", "location": {"left": 220, "top": 120, "width": 90, "height": 36}}
  ]],
  "low_text_confidence": false
}
```

### Field reference

- `paragraphs`: strings in reading order. A single `text` string can be used instead. When both are present, `paragraphs` is treated as the full paragraph text.
- `paragraph_styles`: optional style array. Supported values are `title`, `subtitle`, and `body`; missing or invalid values use the body style.
- `positioned_words`: optional list of text blocks/words. Each item has `text` (also accepts `words`, `content`, or `value`) and a location. The aliases `words_result`, `words`, and `results` are also accepted.
- Locations start at the image's top-left. Use image pixels or normalized values from 0 to 1. The recommended shape is `location: {left, top, width, height}`. `x/y/w/h`, `right/bottom`, `bbox`, or a four-number list `[left, top, width, height]` are also supported. Coordinates must match the orientation and dimensions of the image actually received by the backend.
- `confidence` or `score` may use a 0–100 percentage; `probability` may use a 0–1 decimal. Setting `low_text_confidence: true` asks the UI to remind the user to review recognition. Confidence is only an estimate from your OCR service and does not replace human review.
- `tables` is nested as “tables → rows → cells.” Preserve empty cells; do not shift later columns to make a row shorter.
- `table_layouts` and `table_cells` are ordered by table. A layout may include `column_widths`, `row_heights`, `source_width`, `source_left`, and `source_right`.
- Each `table_cells` item may include `row_start`, `row_end`, `col_start`, `col_end`, `text`, and `location`. End indexes are exclusive: `row_start: 0, row_end: 2` occupies rows 0 and 1. Set both row and column spans correctly for merged cells.

The backend can create a table from `tables` alone. Returning `table_cells` with accurate coordinates helps preserve borders, merged cells, dimensions, and text placement. Missing or out-of-bounds word coordinates are ignored to avoid placing text in the wrong location.

Tables and images have size limits. Return coordinates in the same coordinate space as the input image; do not invent dimensions larger than the page. If your engine rotates, deskews, or crops an image, map coordinates back to the final image sent by the backend.

## Error handling

For retryable connection or service errors, the backend retries the self-hosted service once. If the retry fails, the error is returned to the web app. Low-confidence results are preserved with a review warning. When the service is unavailable or returns no text, the backend does not switch to local or third-party OCR.

See the [OCR benchmark guide](ocr-benchmark.en.md) to compare results across samples. Keep benchmark images and annotations outside the repository.
