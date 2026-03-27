# LiteParse Mistral-Compatible OCR Server

A serverless Mistral-compatible OCR API on Modal, powered by [LiteParse](https://github.com/run-llama/liteparse).
This repo combines a thin Modal/FastAPI wrapper with a TypeScript adapter so you can call LiteParse through a Mistral-style `/v1/ocr` endpoint.

## Key Features

- **Mistral API compatible** - `POST /v1/ocr` accepts Mistral-style document payloads
- **LiteParse-powered** - Uses the LiteParse parser as the OCR engine
- **Two OCR backends** - Default in-process Tesseract, optional PaddleOCR
- **Serverless deployment** - Runs on Modal with scale-to-zero behavior
- **File API support** - Upload, list, retrieve, and sign stored documents
- **Large document handling** - Chunks long PDFs so they stay within OCR limits

## Quick Start

### Prerequisites

- A [Modal](https://modal.com) account
- Modal CLI installed and authenticated

Install and authenticate the Modal CLI:

```bash
pip install modal
modal setup
```

### Create the Auth Secret

The service expects a Modal secret named `mistral-ocr-auth` containing an `AUTH_TOKEN` value:

```bash
modal secret create mistral-ocr-auth AUTH_TOKEN=<your-long-random-token>
```

### Deploy

From the project root:

```bash
modal deploy deploy/deploy.py
```

The Modal app is defined as `mistral-ocr-liteparse`.

## Usage

### Health Check

```bash
GET /healthz
```

### List Available Models

```bash
GET /v1/models
```

Supported model IDs:

- `liteparse-tesseract-latest`
- `liteparse-paddleocr-latest`

### Generate OCR

```bash
POST /v1/ocr
```

Example request:

```bash
curl -X POST https://<your-modal-endpoint>/v1/ocr \
  -H "Authorization: Bearer <your-long-random-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "liteparse-tesseract-latest",
    "document": {
      "document_url": "https://raw.githubusercontent.com/run-llama/liteparse/main/tests/fixtures/scanned.pdf"
    },
    "pages": [0, 1]
  }'
```

Supported document inputs:

- `document.document_url`
- `document.image_url`
- `document.file_content`
- `document.file_id`
- `document.fileId`

The response includes:

- `pages`
- `model`
- `usage_info`

### File API

This service also exposes stored-file endpoints used by the adapter:

- `POST /v1/files`
- `GET /v1/files`
- `GET /v1/files/{file_id}`
- `DELETE /v1/files/{file_id}`
- `GET /v1/files/{file_id}/content`
- `GET /v1/files/{file_id}/url`

Uploads accept `purpose`, `expiry`, and `visibility` form fields. Expired files are cleaned up automatically by a scheduled Modal job.

## Project Structure

```text
.
├── deploy/
│   ├── deploy.py        # Modal app, FastAPI routes, auth, file storage
│   └── adapter.ts       # Mistral-compatible OCR adapter
└── liteparse/
    ├── src/             # LiteParse core library
    ├── ocr/             # Example OCR servers
    └── OCR_API_SPEC.md  # OCR server contract used by LiteParse
```

## Development Notes

- `deploy/deploy.py` is the main entry point for the Modal app.
- `deploy/adapter.ts` translates Mistral OCR requests into LiteParse parser calls.
- `liteparse/ocr/paddleocr/` and `liteparse/ocr/easyocr/` are reference OCR servers that follow the LiteParse OCR API spec.

## Credits

- [LiteParse](https://github.com/run-llama/liteparse)
- [Modal](https://modal.com)
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)
- [Tesseract](https://github.com/tesseract-ocr/tesseract)
