import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
import uuid
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import modal
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile

app = modal.App("mistral-ocr-liteparse")
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
ENABLE_MEMORY_SNAPSHOT = os.environ.get("ENABLE_MEMORY_SNAPSHOT", "").lower() in {
    "1",
    "true",
    "yes",
}
FILE_SIGNING_SECRET = os.environ.get(
    "MISTRAL_FILE_SIGNING_SECRET",
    "liteparse-mistral-file-signing-secret",
).encode("utf-8")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN")
FILES_ROOT = Path("/root/mistral-files")
FILES_VOLUME = modal.Volume.from_name("mistral-ocr-files", create_if_missing=True)
AUTH_SECRET = modal.Secret.from_name("mistral-ocr-auth")
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
CRON_TIMEZONE = "Asia/Kuala_Lumpur"
PADDLEOCR_MODEL_NAMES = {
    "liteparse-paddleocr-latest",
}
MODEL_LIST_CREATED_AT = int(time.time())
PADDLEOCR_SERVER_URL = "http://127.0.0.1:8829/ocr"
PADDLEOCR_HEALTH_URL = "http://127.0.0.1:8829/health"
PADDLEOCR_SERVER_SCRIPT = REPO_ROOT / "liteparse" / "ocr" / "paddleocr" / "server.py"
PADDLEOCR_SERVER_SCRIPT_CANDIDATES = (
    PADDLEOCR_SERVER_SCRIPT,
    Path("/root/liteparse/ocr/paddleocr/server.py"),
    Path("/liteparse/ocr/paddleocr/server.py"),
)
PADDLEOCR_SERVER_PROCESS: subprocess.Popen | None = None
PADDLEOCR_SERVER_LOCK = threading.Lock()


def _validate_file_id(file_id: str) -> str:
    try:
        return str(uuid.UUID(str(file_id)))
    except Exception as exc:  # pragma: no cover - defensive validation
        raise HTTPException(status_code=404, detail="File not found") from exc


def _file_dir(file_id: str) -> Path:
    return FILES_ROOT / _validate_file_id(file_id)


def _meta_path(file_id: str) -> Path:
    return _file_dir(file_id) / "meta.json"


def _content_path(file_id: str) -> Path:
    return _file_dir(file_id) / "content"


def _load_meta(file_id: str, *, expire_if_needed: bool = True) -> dict | None:
    FILES_VOLUME.reload()
    meta_file = _meta_path(file_id)
    if not meta_file.exists():
        return None

    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    if expire_if_needed and _expire_file_if_needed(_file_dir(file_id), meta):
        FILES_VOLUME.commit()

    return meta


def _serialize_file_schema(meta: dict) -> dict:
    payload = {
        "id": meta["id"],
        "object": "file",
        "bytes": meta["sizeBytes"],
        "created_at": meta["createdAt"],
        "filename": meta["filename"],
        "purpose": meta["purpose"],
        "sample_type": meta["sampleType"],
        "source": meta["source"],
    }
    for key in ("numLines", "mimetype", "signature", "expiresAt", "visibility"):
        value = meta.get(key)
        if value is not None:
            payload[
                {
                    "numLines": "num_lines",
                    "expiresAt": "expires_at",
                }.get(key, key)
            ] = value
    return payload


def _serialize_get_file(meta: dict) -> dict:
    payload = _serialize_file_schema(meta)
    payload["deleted"] = bool(meta.get("deleted", False))
    return payload


def _serialize_model_card(model_id: str, *, name: str, description: str) -> dict:
    return {
        "id": model_id,
        "object": "model",
        "created": MODEL_LIST_CREATED_AT,
        "ownedBy": "liteparse",
        "capabilities": {},
        "name": name,
        "description": description,
        "type": "base",
    }


def _auth_error(status_code: int, message: str) -> Response:
    return Response(
        content=json.dumps({"error": message}),
        status_code=status_code,
        media_type="application/json",
    )


def _write_meta(meta: dict) -> None:
    file_dir = _file_dir(meta["id"])
    file_dir.mkdir(parents=True, exist_ok=True)
    _meta_path(meta["id"]).write_text(json.dumps(meta), encoding="utf-8")
    FILES_VOLUME.commit()


def _expire_file_if_needed(file_dir: Path, meta: dict, *, now: int | None = None) -> bool:
    if meta.get("deleted"):
        return False

    expires_at = meta.get("expiresAt")
    if not isinstance(expires_at, int):
        return False

    current_time = int(time.time()) if now is None else now
    if expires_at > current_time:
        return False

    meta["deleted"] = True
    content_file = file_dir / "content"
    if content_file.exists():
        content_file.unlink()
    (file_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return True


def _cleanup_expired_files() -> int:
    FILES_VOLUME.reload()
    if not FILES_ROOT.exists():
        return 0

    expired_count = 0
    now = int(time.time())
    for file_dir in FILES_ROOT.iterdir():
        if not file_dir.is_dir():
            continue

        meta_file = file_dir / "meta.json"
        if not meta_file.exists():
            continue

        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if _expire_file_if_needed(file_dir, meta, now=now):
            expired_count += 1

    if expired_count:
        FILES_VOLUME.commit()

    return expired_count


def _is_paddleocr_model(model: str | None) -> bool:
    if not model:
        return False
    return model.strip().lower() in PADDLEOCR_MODEL_NAMES


def _wait_for_paddleocr_server(timeout_seconds: float = 180.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        global PADDLEOCR_SERVER_PROCESS
        if PADDLEOCR_SERVER_PROCESS and PADDLEOCR_SERVER_PROCESS.poll() is not None:
            raise RuntimeError(
                f"PaddleOCR server exited with code {PADDLEOCR_SERVER_PROCESS.returncode}"
            )
        try:
            with urllib.request.urlopen(PADDLEOCR_HEALTH_URL, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(1)
    raise RuntimeError("Timed out waiting for PaddleOCR server to become healthy")
def ensure_paddleocr_server() -> None:
    global PADDLEOCR_SERVER_PROCESS
    with PADDLEOCR_SERVER_LOCK:
        if PADDLEOCR_SERVER_PROCESS and PADDLEOCR_SERVER_PROCESS.poll() is None:
            return

        if PADDLEOCR_SERVER_PROCESS and PADDLEOCR_SERVER_PROCESS.poll() is not None:
            PADDLEOCR_SERVER_PROCESS = None

        server_script = next(
            (candidate for candidate in PADDLEOCR_SERVER_SCRIPT_CANDIDATES if candidate.exists()),
            None,
        )
        if server_script is None:
            raise RuntimeError(
                "Missing PaddleOCR server script: "
                + ", ".join(str(candidate) for candidate in PADDLEOCR_SERVER_SCRIPT_CANDIDATES)
            )

        PADDLEOCR_SERVER_PROCESS = subprocess.Popen(
            [sys.executable, str(server_script)],
            cwd=str(server_script.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            _wait_for_paddleocr_server()
        except Exception:
            if PADDLEOCR_SERVER_PROCESS and PADDLEOCR_SERVER_PROCESS.poll() is None:
                PADDLEOCR_SERVER_PROCESS.kill()
            PADDLEOCR_SERVER_PROCESS = None
            raise


def _sign_token(file_id: str, expiry_hours: int) -> str:
    expires_at = int(time.time()) + max(1, expiry_hours) * 3600
    payload = json.dumps({"file_id": file_id, "exp": expires_at}, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(FILE_SIGNING_SECRET, payload, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    )


def _verify_token(token: str, file_id: str) -> bool:
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        expected = hmac.new(FILE_SIGNING_SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, sig):
            return False
        data = json.loads(payload.decode("utf-8"))
        return data.get("file_id") == file_id and int(data.get("exp", 0)) > int(time.time())
    except Exception:
        return False


def _list_file_metas(*, expire_if_needed: bool = True) -> list[dict]:
    FILES_VOLUME.reload()
    if not FILES_ROOT.exists():
        return []

    metas: list[dict] = []
    expired_count = 0
    now = int(time.time())
    for file_dir in FILES_ROOT.iterdir():
        if not file_dir.is_dir():
            continue
        meta_file = file_dir / "meta.json"
        if not meta_file.exists():
            continue
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if expire_if_needed and _expire_file_if_needed(file_dir, meta, now=now):
            expired_count += 1
        metas.append(meta)

    if expire_if_needed and expired_count:
        FILES_VOLUME.commit()

    return metas

# Build image with Node.js and LiteParse dependencies
image = (
    modal.Image.debian_slim()
    # Install node 20
    .run_commands(
        "apt-get update && apt-get install -y curl gnupg libglib2.0-0 libnss3 libnss3-dev libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libasound2",
        "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
        "apt-get install -y nodejs",
        "apt-get install -y poppler-utils",
        "apt-get install -y imagemagick",
        "apt-get install -y ghostscript",
        "sed -i 's/domain=\"coder\" rights=\"none\" pattern=\"PDF\"/domain=\"coder\" rights=\"read|write\" pattern=\"PDF\"/' /etc/ImageMagick-6/policy.xml || true",
        "sed -i 's/domain=\"coder\" rights=\"none\" pattern=\"PDF\"/domain=\"coder\" rights=\"read|write\" pattern=\"PDF\"/' /etc/ImageMagick-7/policy.xml || true",
        "apt-get install -y libreoffice", # Optional: for non-PDF office formats
        "pip install --index-url https://www.paddlepaddle.org.cn/packages/stable/cpu/ paddlepaddle",
        "pip install fastapi python-multipart uvicorn numpy pillow paddleocr"
    )
    # Copy liteparse source
    .add_local_dir(str(REPO_ROOT / "liteparse"), "/root/liteparse", copy=True)
    # Copy deploy directory containing our adapter and its package.json
    .add_local_dir(str(BASE_DIR), "/root/deploy", copy=True)
    # Install dependencies for both
    .run_commands(
        "cd /root/liteparse && npm install",
        "cd /root/deploy && npm install"
    )
)

cleanup_image = modal.Image.debian_slim().pip_install("fastapi")

web_app = FastAPI()


@web_app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path == "/healthz":
        return await call_next(request)

    if not AUTH_TOKEN:
        return _auth_error(500, "AUTH_TOKEN is not configured")

    authorization = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return _auth_error(401, "Missing bearer token")

    presented_token = authorization[len(prefix) :].strip()
    if not presented_token or not hmac.compare_digest(presented_token, AUTH_TOKEN):
        return _auth_error(401, "Unauthorized")

    return await call_next(request)

@web_app.get("/healthz")
async def healthz():
    return {"ok": True}

@web_app.post("/v1/files")
async def upload_file(
    file: UploadFile = File(...),
    purpose: str = Form("ocr"),
    expiry: int | None = Form(None),
    visibility: str = Form("workspace"),
):
    FILES_ROOT.mkdir(parents=True, exist_ok=True)
    file_id = str(uuid.uuid4())
    file_dir = _file_dir(file_id)
    file_dir.mkdir(parents=True, exist_ok=True)
    content_path = _content_path(file_id)
    hasher = hashlib.sha256()
    size = 0

    try:
        with content_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File too large")
                out.write(chunk)
                hasher.update(chunk)

        created_at = int(time.time())
        meta = {
            "id": file_id,
            "object": "file",
            "sizeBytes": size,
            "createdAt": created_at,
            "filename": file.filename or f"{file_id}.bin",
            "purpose": purpose or "ocr",
            "sampleType": "batch_request",
            "mimetype": file.content_type,
            "source": "upload",
            "signature": hasher.hexdigest(),
            "visibility": visibility,
            "deleted": False,
        }
        if expiry is not None:
            meta["expiresAt"] = created_at + max(1, expiry) * 3600

        _write_meta(meta)
        return _serialize_file_schema(meta)
    except HTTPException:
        shutil.rmtree(file_dir, ignore_errors=True)
        FILES_VOLUME.commit()
        raise
    except Exception as exc:
        shutil.rmtree(file_dir, ignore_errors=True)
        FILES_VOLUME.commit()
        return Response(
            content=json.dumps({"error": str(exc)}),
            status_code=500,
            media_type="application/json",
        )

@web_app.get("/v1/files")
async def list_files(
    page: int = Query(0, alias="page"),
    page_size: int = Query(100, alias="page_size"),
    include_total: bool = Query(True, alias="include_total"),
    sample_type: list[str] | None = Query(None, alias="sample_type"),
    source: list[str] | None = Query(None, alias="source"),
    search: str | None = Query(None, alias="search"),
    purpose: str | None = Query(None, alias="purpose"),
    mimetypes: list[str] | None = Query(None, alias="mimetypes"),
):
    metas = [meta for meta in _list_file_metas() if not meta.get("deleted")]

    if sample_type:
        metas = [meta for meta in metas if meta.get("sampleType") in sample_type]
    if source:
        metas = [meta for meta in metas if meta.get("source") in source]
    if search:
        needle = search.lower()
        metas = [meta for meta in metas if needle in str(meta.get("filename", "")).lower()]
    if purpose:
        metas = [meta for meta in metas if meta.get("purpose") == purpose]
    if mimetypes:
        metas = [meta for meta in metas if meta.get("mimetype") in mimetypes]

    metas.sort(key=lambda item: item.get("createdAt", 0), reverse=True)
    start = max(0, page) * max(1, page_size)
    end = start + max(1, page_size)
    data = [_serialize_file_schema(meta) for meta in metas[start:end]]
    response = {"data": data, "object": "list"}
    if include_total:
        response["total"] = len(metas)
    return response

@web_app.get("/v1/files/{file_id}")
async def retrieve_file(file_id: str):
    meta = _load_meta(file_id)
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    return _serialize_get_file(meta)

@web_app.delete("/v1/files/{file_id}")
async def delete_file(file_id: str):
    meta = _load_meta(file_id, expire_if_needed=False)
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")

    meta["deleted"] = True
    content_path = _content_path(file_id)
    if content_path.exists():
        content_path.unlink()
    _write_meta(meta)
    return {"id": meta["id"], "object": "file", "deleted": True}

@web_app.get("/v1/files/{file_id}/content")
async def download_file(file_id: str, token: str | None = Query(None, alias="token")):
    meta = _load_meta(file_id)
    if not meta or meta.get("deleted"):
        raise HTTPException(status_code=404, detail="File not found")
    if token and not _verify_token(token, file_id):
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    content_path = _content_path(file_id)
    if not content_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    media_type = meta.get("mimetype") or "application/octet-stream"
    headers = {
        "Content-Disposition": f'attachment; filename="{meta.get("filename", file_id)}"',
    }
    return Response(
        content=content_path.read_bytes(),
        media_type=media_type,
        headers=headers,
    )

@web_app.get("/v1/files/{file_id}/url")
async def get_signed_url(file_id: str, request: Request, expiry: int = Query(24, alias="expiry")):
    meta = _load_meta(file_id)
    if not meta or meta.get("deleted"):
        raise HTTPException(status_code=404, detail="File not found")

    token = _sign_token(file_id, expiry)
    base_url = str(request.base_url).rstrip("/")
    return {"url": f"{base_url}/v1/files/{file_id}/content?token={token}"}


@web_app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            _serialize_model_card(
                "liteparse-tesseract-latest",
                name="LiteParse Tesseract OCR",
                description="Mistral-compatible OCR backed by in-process Tesseract.",
            ),
            _serialize_model_card(
                "liteparse-paddleocr-latest",
                name="LiteParse PaddleOCR",
                description="Mistral-compatible OCR backed by PaddleOCR.",
            ),
        ],
    }


@web_app.post("/v1/ocr")
async def ocr(request: Request):
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({"error": "Invalid JSON request body"}),
            status_code=400,
            media_type="application/json"
        )
    
    model = body.get("model") if isinstance(body, dict) else None
    if _is_paddleocr_model(model):
        try:
            ensure_paddleocr_server()
        except Exception as exc:
            return Response(
                content=json.dumps({
                    "error": "Failed to start PaddleOCR backend",
                    "message": str(exc),
                }),
                status_code=500,
                media_type="application/json",
            )

    # Run the TS adapter using tsx
    cmd = ["npx", "tsx", "/root/deploy/adapter.ts"]
    
    result = subprocess.run(
        cmd, 
        capture_output=True, 
        text=True, 
        input=json.dumps(body),
        cwd="/root/deploy",
        env={**os.environ, "NODE_NO_WARNINGS": "1"}
    )
    
    if result.returncode != 0:
        error_msg = result.stderr or result.stdout or "Unknown internal error"
        return Response(
            content=json.dumps({
                "error": "OCR Adapter Error",
                "message": error_msg,
                "returncode": result.returncode
            }),
            status_code=500,
            media_type="application/json"
        )
        
    return Response(
        content=result.stdout,
        status_code=200,
        media_type="application/json"
    )

@app.function(
    image=image, 
    cpu=3.0, 
    memory=4096, 
    timeout=1800,
    scaledown_window=2,
    enable_memory_snapshot=ENABLE_MEMORY_SNAPSHOT,
    min_containers=0,
    volumes={"/root/mistral-files": FILES_VOLUME},
    secrets=[AUTH_SECRET],
)
@modal.asgi_app(label="mistral-ocr")
def run():
    return web_app


@app.function(
    image=cleanup_image,
    cpu=0.25,
    memory=256,
    timeout=300,
    min_containers=0,
    volumes={"/root/mistral-files": FILES_VOLUME},
    schedule=modal.Cron("0 3 * * *", timezone=CRON_TIMEZONE),
)
def cleanup_expired_files():
    expired_count = _cleanup_expired_files()
    print(f"expired_files_deleted: {expired_count}")
    return {"expired_files_deleted": expired_count}
