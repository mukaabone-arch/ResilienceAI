"""Evidence file storage and scanning (spec §14, §19: files in object storage, metadata in PostgreSQL).

`LocalStore` is for development and the pilot demo. Production plugs in an
object store with server-side encryption (e.g. S3 with SSE-KMS, or the bank's
approved repository) behind the same two methods. File bytes never go into the
database; the checksum recorded at upload is re-checked on every download.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from ..config import settings

ALLOWED_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "gif", "webp", "txt", "log", "csv", "json", "xml",
    "xlsx", "xls", "docx", "doc", "pptx", "msg", "eml", "zip",
}
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class EvidenceRejected(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(name: str) -> str:
    base = os.path.basename(name or "").strip().replace("\x00", "")
    base = re.sub(r"[^\w.\- ()]+", "_", base)[:200].strip(" .")
    return base or "evidence"


def check_file(filename: str, data: bytes) -> None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise EvidenceRejected(f"Files of type .{ext or '(none)'} can't be uploaded. Allowed: "
                               + ", ".join(sorted(ALLOWED_EXTENSIONS)))
    if not data:
        raise EvidenceRejected("The file is empty")
    if len(data) > settings.evidence_max_mb * 1024 * 1024:
        raise EvidenceRejected(f"The file is larger than the {settings.evidence_max_mb} MB limit")


class Scanner(Protocol):
    def scan(self, filename: str, data: bytes) -> str: ...


class BuiltinScanner:
    """Placeholder until the bank's scanner (e.g. ClamAV via clamd, or an ICAP gateway) is connected.
    Detects the industry-standard EICAR test file, so the rejection path can be demonstrated; everything
    else is recorded as NotScanned rather than claimed clean."""
    def scan(self, filename: str, data: bytes) -> str:
        return "Infected" if EICAR in data else "NotScanned"


scanner: Scanner = BuiltinScanner()


class Store(Protocol):
    backend: str
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, ref: str) -> bytes: ...


class LocalStore:
    backend = "local"

    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _path(self, ref: str) -> Path:
        p = (self.root / ref).resolve()
        if self.root not in p.parents:
            raise EvidenceRejected("Invalid storage reference")
        return p

    def put(self, key: str, data: bytes) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)          # atomic: never a half-written evidence file
        return key

    def get(self, ref: str) -> bytes:
        return self._path(ref).read_bytes()


def store() -> Store:
    if settings.evidence_store == "local":
        return LocalStore(settings.evidence_dir)
    raise RuntimeError(f"Evidence store '{settings.evidence_store}' is not configured in this build")


def object_key(tenant_id: UUID, event_id: UUID) -> str:
    return f"{tenant_id}/{event_id}/{uuid4()}"
