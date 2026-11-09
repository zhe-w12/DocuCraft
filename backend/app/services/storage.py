from __future__ import annotations

import os
import mimetypes
import shutil
from pathlib import Path
from urllib.parse import quote


def is_non_empty_file(path: Path) -> bool:
    """Treat only readable, non-zero files as local storage hits."""

    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


class AliyunOssStorage:
    def __init__(self) -> None:
        try:
            import oss2
        except ImportError as exc:
            raise RuntimeError("STORAGE_BACKEND=aliyun_oss 时需要安装 oss2") from exc

        endpoint = os.getenv("OSS_ENDPOINT", "").strip()
        bucket_name = os.getenv("OSS_BUCKET_NAME", "").strip()
        access_key_id = os.getenv("OSS_ACCESS_KEY_ID", "").strip()
        access_key_secret = os.getenv("OSS_ACCESS_KEY_SECRET", "").strip()
        if not all((endpoint, bucket_name, access_key_id, access_key_secret)):
            raise RuntimeError(
                "STORAGE_BACKEND=aliyun_oss 时必须配置 OSS_ENDPOINT、OSS_BUCKET_NAME、"
                "OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET"
            )
        if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
            endpoint = f"https://{endpoint}"
        self._bucket = oss2.Bucket(oss2.Auth(access_key_id, access_key_secret), endpoint, bucket_name)
        self._prefix = os.getenv("OSS_OBJECT_PREFIX", "scan-convert").strip("/")
        self._expires = int(os.getenv("OSS_URL_EXPIRE_SECONDS", "3600"))

    def publish(self, local_path: Path, *, inline: bool = False) -> str:
        object_name = f"{self._prefix}/{local_path.name}" if self._prefix else local_path.name
        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        self._bucket.put_object_from_file(
            object_name,
            str(local_path),
            headers={
                "Content-Type": content_type,
                "Content-Disposition": (
                    f"inline; filename*=UTF-8''{quote(local_path.name)}"
                    if inline
                    else f"attachment; filename*=UTF-8''{quote(local_path.name)}"
                ),
            },
        )
        signed_url = self.signed_url(local_path.name)
        local_path.unlink(missing_ok=True)
        return signed_url

    def signed_url(self, name: str) -> str:
        object_name = f"{self._prefix}/{Path(name).name}" if self._prefix else Path(name).name
        return self._bucket.sign_url("GET", object_name, self._expires)

    def delete_published(self, name: str) -> None:
        object_name = f"{self._prefix}/{Path(name).name}" if self._prefix else Path(name).name
        self._bucket.delete_object(object_name)

    def download(self, name: str, destination: Path) -> None:
        object_name = f"{self._prefix}/{Path(name).name}" if self._prefix else Path(name).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._bucket.get_object_to_file(object_name, str(destination))


class DocumentStorage:
    def __init__(self) -> None:
        configured = os.getenv("STORAGE_DIR", "storage")
        self.root = Path(configured)
        if not self.root.is_absolute():
            self.root = Path(__file__).resolve().parents[2] / self.root
        self.root.mkdir(parents=True, exist_ok=True)
        backend = os.getenv("STORAGE_BACKEND", "local").strip().lower()
        self._remote = AliyunOssStorage() if backend == "aliyun_oss" else None

    def path(self, name: str) -> Path:
        return self.root / name

    def publish(self, local_path: Path, *, inline: bool = False) -> str:
        if self._remote:
            return self._remote.publish(local_path, inline=inline)
        return self.url(local_path.name)

    def delete_published(self, name: str) -> None:
        safe_name = Path(name).name
        if self._remote:
            self._remote.delete_published(safe_name)
            return
        self.path(safe_name).unlink(missing_ok=True)

    def published_url(self, name: str) -> str:
        safe_name = Path(name).name
        if self._remote:
            return self._remote.signed_url(safe_name)
        return self.url(safe_name)

    def download_published(self, name: str, destination: Path) -> None:
        safe_name = Path(name).name
        local_path = self.path(safe_name)
        if is_non_empty_file(local_path):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_path, destination)
            return
        if self._remote:
            self._remote.download(safe_name, destination)
            return
        raise FileNotFoundError(safe_name)

    def url(self, name: str) -> str:
        base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        relative = f"/files/{name}"
        return f"{base}{relative}" if base else relative
