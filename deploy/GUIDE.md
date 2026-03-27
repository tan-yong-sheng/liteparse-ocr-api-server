# Modal Deployment Guide

This guide covers deploying the Mistral-compatible OCR endpoint on Modal with private access enforced by an `AUTH_TOKEN`.

## 1) What You Get

- One Modal deployment serving the OCR API.
- Scale-to-zero by default (`min_containers=0`).
- Mistral-compatible `/v1/ocr` and `/v1/models` endpoints.
- Private access using a bearer token stored in a Modal Secret.
- Optional PaddleOCR backend alongside the default Tesseract backend.

## 2) Files

- `deploy/deploy.py` - Modal entrypoint and FastAPI app
- `deploy/adapter.ts` - Mistral-compatible request adapter
- `deploy/README.md` - short usage reference

## 3) One-Time Setup

Install the Modal CLI if needed:

```bash
pip install modal
modal setup
```

Create a strong random token and store it in a Modal Secret:

```bash
modal secret create mistral-ocr-auth AUTH_TOKEN=<your-long-random-token>
```

This token is what the server expects in:

```bash
Authorization: Bearer <your-long-random-token>
```

## 4) Deploy

From the repo root:

```bash
modal deploy deploy/deploy.py
```

The app mounts the `mistral-ocr-auth` secret at runtime and exposes the private Modal URL.

## 5) Use the Endpoint

### cURL

```bash
curl -X POST https://<your-modal-app>.modal.run/v1/ocr \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-long-random-token>" \
  -d '{
    "model": "liteparse-tesseract-latest",
    "document": {
      "document_url": "https://raw.githubusercontent.com/run-llama/liteparse/main/tests/fixtures/scanned.pdf"
    }
  }'
```

### Mistral SDK

Point the SDK at your Modal URL and use the same token as `MISTRAL_API_KEY`:

```python
from mistralai.client import Mistral

client = Mistral(
    api_key="YOUR_AUTH_TOKEN",
    server_url="https://<your-modal-app>.modal.run",
)
```

## 6) Model Discovery

The endpoint exposes models at:

```bash
GET /v1/models
```

Current supported model IDs:

- `liteparse-tesseract-latest`
- `liteparse-paddleocr-latest`

## 7) Notes

- `AUTH_TOKEN` is not a Mistral SDK variable. It is the application token enforced by the FastAPI middleware.
- `MISTRAL_API_KEY` is only needed on the client side so the SDK can send the bearer token.
- OCR remains enabled for DOCX/PPTX because the endpoint is designed to stay Mistral-compatible.
- Large PDFs are chunked internally, and the app still scales to zero when idle.

## 8) Quick Tests

Check that the endpoint is private:

```bash
curl -i https://<your-modal-app>.modal.run/v1/models
```

You should get `401 Unauthorized` without the token.

Check that the token works:

```bash
curl -sS https://<your-modal-app>.modal.run/v1/models \
  -H "Authorization: Bearer <your-long-random-token>"
```

## 9) Troubleshooting

- `401 Unauthorized`
  - Make sure `Authorization: Bearer <token>` is present.
  - Confirm the token matches the one stored in `mistral-ocr-auth`.
- `500 AUTH_TOKEN is not configured`
  - The Modal secret was not attached or does not contain `AUTH_TOKEN`.
- `404 File not found`
  - The file ID is missing, deleted, or expired.

