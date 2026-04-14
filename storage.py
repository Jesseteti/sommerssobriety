import os
import hashlib
from pathlib import Path

BASE_STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "storage"))
BASE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def upload_bytes(bucket: str, object_path: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
    """
    Save bytes to local storage instead of Supabase Storage.
    """
    file_path = BASE_STORAGE_DIR / bucket / object_path
    file_path.parent.mkdir(parents=True, exist_ok=True)

    with open(file_path, "wb") as f:
        f.write(data)

    return {
        "sha256": sha256_bytes(data),
        "size_bytes": len(data),
        "file_path": str(file_path),
        "bucket": bucket,
        "object_path": object_path,
        "content_type": content_type,
    }


def create_signed_url(bucket: str, object_path: str, expires_in_seconds: int = 300) -> str:
    """
    Replace Supabase signed URLs with an internal app route.
    """
    return f"/files/{bucket}/{object_path}"