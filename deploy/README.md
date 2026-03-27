# Mistral-Compatible OCR Endpoint on Modal

This project deploys a serverless OCR endpoint to [Modal.com](https://modal.com) that implements the Mistral OCR API (`/v1/ocr`) using [LiteParse](https://github.com/run-llama/liteparse) as the engine.

## Features

- **Mistral API Compatibility**: Supports `document_url` and `file_content` (base64) in the Mistral request format.
- **Serverless Scaling**: Powered by Modal, scales to zero when not in use.
- **LiteParse Engine**: High-quality document parsing with optional OCR.

## Prerequisites

- [Modal account](https://modal.com)
- Modal CLI installed and configured: `pip install modal && modal setup`

## Deployment

1. Make sure you are in the project root directory.
2. Run the following command:

```bash
modal deploy deploy/deploy.py
```

3. Once deployed, Modal will provide a URL like `https://<your-username>--v1-ocr.modal.run`.

## Usage

You can now use this endpoint with any Mistral-compatible client.

### Private Access

The endpoint is protected with a bearer token stored in a Modal Secret named `mistral-ocr-auth`.

Create the secret with:

```bash
modal secret create mistral-ocr-auth AUTH_TOKEN=<your-long-random-token>
```

When you call this endpoint through the Mistral SDK, set `MISTRAL_API_KEY` to the same token value so the SDK sends the expected bearer token.

### Available Models

The endpoint exposes the current supported model IDs through `GET /v1/models`:

- `liteparse-tesseract-latest` - default model, backed by in-process Tesseract OCR
- `liteparse-paddleocr-latest` - PaddleOCR-backed OCR model

These are the model names you can pass to the `model` field in `/v1/ocr`.

### Example Request (curl)

```bash
curl -X POST https://<your-username>--v1-ocr.modal.run \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your-long-random-token>" \
  -d '{
    "model": "liteparse-tesseract-latest",
    "document": {
      "document_url": "https://raw.githubusercontent.com/run-llama/liteparse/main/tests/fixtures/scanned.pdf"
    }
  }'
```

### Supported Request Fields

- `model`: (Optional) Model name. Supported values include `liteparse-tesseract-latest` and `liteparse-paddleocr-latest`.
- `document`:
  - `document_url`: URL to a PDF or image.
  - `file_content`: Base64 encoded document bytes.
- `pages`: (Optional) Array of 0-indexed page numbers to process.
- Documents larger than 1000 pages are chunked internally so the public endpoint stays Mistral-compatible while still handling large PDFs.
- Expired uploads are also swept daily by a scheduled cleanup job at `03:00` Asia/Kuala_Lumpur time.
