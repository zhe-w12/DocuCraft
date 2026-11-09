# Backend guide

English | [简体中文](README.md)

The DocuCraft backend uses FastAPI for browser APIs, OCR requests, document generation, file storage, and downloads. Production Compose uses `generic_http` and sends scans only to the OCR endpoint configured by the operator.

## Requirements

- Python 3.13
- A repository-root `.env` file, copied from [`backend/.env.example`](.env.example)
- LibreOffice `soffice` available to the system when converting DOCX files to PDF
- A self-hosted OCR HTTP service reachable from the backend for real scan recognition

## Local development

Create the development environment file at the repository root. Mock OCR is useful for startup and UI development; it does not represent the accuracy of a real engine.

```powershell
Copy-Item backend/.env.example .env
Set-Location backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

Linux/macOS:

```sh
cp backend/.env.example .env
cd backend
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

The default API URL is `http://127.0.0.1:5000`. Health check: `GET /health`. See the [web frontend guide](../frontend/README.md) to start the client.

## Configuration

The backend reads `.env` from the repository root. Never copy `.env` or real credentials to the frontend, a Docker image, or a Git commit. Common variables are listed below; see [`.env.example`](.env.example) for all defaults.

| Variable | Description |
| --- | --- |
| `HOST` / `PORT` | API bind address and port. Local development commonly uses `127.0.0.1:5000`; containers follow Compose settings. |
| `OCR_PROVIDER` | `mock` for local development or `generic_http` for a self-hosted OCR service. |
| `OCR_ENDPOINT` | Full HTTP(S) URL for the self-hosted OCR service. Required in production. |
| `OCR_API_KEY` | Optional Bearer token. Keep it in the backend environment only. |
| `STORAGE_BACKEND` | `local` for backend disk or `aliyun_oss` for the configured object store. |
| `STORAGE_DIR` | Local storage path. Production deployments should mount a persistent directory. |
| `AI_PROVIDER` / `AI_API_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | Optional AI writing model settings. The model key belongs on the backend only. |
| `TAVILY_API_KEY` | Optional LangChain web search key. Keep it on the backend; once configured, enable search in the assistant composer. |
| `MAX_UPLOAD_MB` / `MAX_SOURCE_IMAGE_MB` | API upload and source-image size limits. |
| `OCR_MAX_CONCURRENT` / `OCR_REQUESTS_PER_MINUTE` | OCR concurrency and request rate limits. |

`VITE_API_BASE_URL` is a frontend build variable, not a backend setting. See the [self-hosted OCR guide](../docs/self-hosted-ocr.en.md) for the OCR contract.

## AI web search

Set `TAVILY_API_KEY` in the repository-root `.env` file and restart the backend. For Docker Compose, rebuild the backend image:

```sh
docker compose -f backend/docker-compose.yml up --build
```

Then enable “Web search” in the assistant composer. When disabled, the assistant does not call the search tool. When enabled, the LangChain agent can call Tavily up to 3 times per request, with up to 5 results per search. Answers include numbered citations and source links. Search queries are sent to Tavily; keep the key in the backend environment. The configured chat model must support Function Calling.

## OCR request flow

1. The frontend sends a scan to `POST /api/v1/documents/convert`.
2. The backend validates file type, size, and image limits, then selects the configured provider.
3. `generic_http` sends a multipart image request to `OCR_ENDPOINT`, with optional Bearer authentication.
4. The backend parses paragraphs, positioned words, and tables, creates editable documents, and stores files and metadata.
5. The frontend renders the returned document for review, editing, and export.

Production does not switch to a local or third-party OCR engine if the configured service is unavailable. Retryable failures receive a retry according to the provider policy; final failures are returned to the web app. OCR result, coordinate, and table types, the generic HTTP adapter, provider selection, and shared recognition logic are all in `app/services/ocr.py`. The provider factory allows only `mock` (development) and `generic_http` (self-hosted OCR).

## Main APIs

| Method and path | Purpose |
| --- | --- |
| `GET /health` | Health status, source fingerprint, and upload limits. |
| `GET /api/v1/assistant/status` | Check whether the AI assistant is configured. |
| `POST /api/v1/assistant/chat` | Send a chat or streaming generation request. |
| `POST /api/v1/assistant/export/docx` | Export an AI document as Word. |
| `POST /api/v1/assistant/export/pdf` | Export an AI document as PDF. |
| `POST /api/v1/documents/convert` | Upload a scan and create recognized documents. |
| `POST /api/v1/documents/export-edited` | Export an edited OCR document. |

Request fields and response models are defined by `backend/app/models.py` and the API implementation. When changing an API contract, update the frontend client, compatibility version, and relevant checks together.

## Docker deployment

### Local Compose

Create `.env` in the repository root and run:

```sh
docker compose -f backend/docker-compose.yml up --build
```

### Linux production Compose

The production example uses host networking and separate persistent storage. Prepare the deployment directory and environment file, and set `OCR_ENDPOINT` in `.env`:

```sh
export ISCAN_ROOT=/opt/docucraft
mkdir -p "$ISCAN_ROOT/backend/storage" "$ISCAN_ROOT/backend/storage_metadata"
# Copy backend/.env.example to "$ISCAN_ROOT/.env" and edit the required settings.
docker compose --env-file "$ISCAN_ROOT/.env" \
  -f backend/docker-compose.production.yml up -d --build
```

Production fixes `OCR_PROVIDER` to `generic_http`. If the OCR service is on the same host, confirm that the address matches the network mode. If it runs elsewhere, use a private DNS/IP address or HTTPS URL reachable from the backend. Inside a container, `127.0.0.1` does not refer to another container or remote server.

Before going live:

1. Use a dedicated runtime account and grant only the file permissions it needs.
2. Keep `.env` outside the source repository and ensure OCR, AI, and object-storage keys are not committed.
3. Configure persistent storage and a backup policy for user files.
4. Expose the web app and API through an HTTPS reverse proxy; do not publish an unprotected debug port.
5. Review firewall rules, upload limits, concurrency, and rate limits for your workload.
6. After startup, check `/health` and separately verify the web app, OCR, document export, and file downloads.

CI builds matched web and backend release artifacts. Use artifacts from the same CI run in production and follow the included `DEPLOY.txt` to avoid mismatched versions. See the [Nginx example](../deploy/README.md) for static web hosting.

## Tests and development checks

```sh
cd backend
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

The OCR benchmark script is `scripts/benchmark_ocr.py`. Use authorized sample images; keep images, annotations, recognition output, and reports outside the repository. See the [OCR benchmarking guide](../docs/ocr-benchmark.en.md).

## Troubleshooting

- **`OCR_ENDPOINT` is missing:** production Compose refuses to start. Set a complete, reachable URL in the deployment `.env`.
- **OCR connection error:** test DNS, port, TLS certificate, and access rules from the backend runtime; the browser does not make the OCR request.
- **OCR returns 401/403:** confirm the service expects `Authorization: Bearer ...` and that `OCR_API_KEY` matches.
- **Text is present but layout is poor:** have the OCR service return `positioned_words` and table/cell coordinates. Text-only results do not contain source-page geometry.
- **PDF export fails:** make sure `soffice` is installed and executable by the backend process; check backend logs using the request ID.
- **Files disappear after restart:** verify that Compose volumes point to persistent host directories rather than the container's temporary filesystem.

