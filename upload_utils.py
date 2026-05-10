import os
import re

from fastapi import HTTPException, UploadFile, status


def safe_filename(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    name = re.sub(r"\s+", " ", name)
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename")
    return name


def upload_max_bytes() -> int:
    return int(os.getenv("UPLOAD_MAX_BYTES", str(25 * 1024 * 1024)))


def allowed_upload_extensions() -> set[str]:
    raw = os.getenv("ALLOWED_UPLOAD_EXTENSIONS", "pdf,txt,docx")
    return {ext.strip().lower().lstrip(".") for ext in raw.split(",") if ext.strip()}


def read_upload_limited(file: UploadFile, max_bytes: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds upload limit of {max_bytes} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)
