from __future__ import annotations

import hashlib
from pathlib import Path


def backend_source_fingerprint() -> str:
    """Return the stable fingerprint used to identify a deployed backend."""
    backend_root = Path(__file__).resolve().parents[1]
    source_files = sorted((backend_root / "app").rglob("*.py"))
    source_files.append(backend_root / "requirements.txt")
    digest = hashlib.sha256()
    for path in source_files:
        digest.update(path.relative_to(backend_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]
