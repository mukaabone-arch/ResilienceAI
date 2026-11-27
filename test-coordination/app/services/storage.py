"""Evidence file storage and malware scanning (spec §14, §19: files in object storage, metadata in PostgreSQL).

Stores
- `LocalStore`: development and demos only (refused in production).
- `S3Store`: any S3-compatible store (AWS S3, MinIO and others). Every object is written with server-side
  encryption and, when a retention period is set, an Object Lock retain-until date, so no one (administrators
  included, in COMPLIANCE mode) can delete or overwrite evidence before it expires. A legal hold keeps a file
  past its retention date. The SHA-256 checksum recorded at upload is sent with the write (the store rejects a
  corrupted upload) and re-checked on every download.

Scanners
- `BuiltinScanner`: recognises only the EICAR test file (development; refused in production).
- `ClamAVScanner`: streams the file to a clamd daemon (INSTREAM). If clamd can't be reached the verdict is
  Pending: the file is stored but can't be opened or accepted until the worker has scanned it.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from .. import config

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
    limit = config.settings.evidence_max_mb
    if len(data) > limit * 1024 * 1024:
        raise EvidenceRejected(f"The file is larger than the {limit} MB limit")


# ---------------------------------------------------------------------------
# Malware scanning
# ---------------------------------------------------------------------------
@dataclass
class ScanResult:
    status: str                 # Clean | Infected | NotScanned | Pending
    engine: str
    detail: str | None = None   # signature name when infected, reason when pending


class ScannerUnavailable(RuntimeError):
    pass


class Scanner(Protocol):
    engine: str
    def scan(self, filename: str, data: bytes) -> ScanResult: ...


class BuiltinScanner:
    """Development only. Detects the industry-standard EICAR test file so the rejection path can be shown;
    everything else is recorded as NotScanned rather than claimed clean."""
    engine = "builtin"

    def scan(self, filename: str, data: bytes) -> ScanResult:
        if EICAR in data:
            return ScanResult("Infected", self.engine, "EICAR test file")
        return ScanResult("NotScanned", self.engine)


class ClamAVScanner:
    """clamd INSTREAM over TCP: 'zINSTREAM\\0', then <4-byte length><chunk> frames, then a zero-length frame."""
    engine = "clamav"
    CHUNK = 64 * 1024

    def __init__(self, host: str, port: int, timeout: float):
        self.host, self.port, self.timeout = host, port, timeout

    def _reply(self, sock: socket.socket) -> str:
        buf = b""
        while not buf.endswith(b"\0"):
            part = sock.recv(4096)
            if not part:
                break
            buf += part
        return buf.rstrip(b"\0").decode("utf-8", "replace").strip()

    def scan(self, filename: str, data: bytes) -> ScanResult:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(b"zINSTREAM\0")
                for i in range(0, len(data), self.CHUNK):
                    chunk = data[i:i + self.CHUNK]
                    s.sendall(struct.pack(">I", len(chunk)) + chunk)
                s.sendall(struct.pack(">I", 0))
                reply = self._reply(s)
        except OSError as e:
            raise ScannerUnavailable(f"ClamAV at {self.host}:{self.port} unreachable: {e}") from e
        # "stream: OK" | "stream: <Signature> FOUND" | "<message> ERROR"
        if reply.endswith("OK"):
            return ScanResult("Clean", self.engine)
        if reply.endswith("FOUND"):
            sig = reply.split(":", 1)[-1].strip()[:-len("FOUND")].strip()
            return ScanResult("Infected", self.engine, sig[:200] or "malware")
        raise ScannerUnavailable(f"ClamAV could not scan the file: {reply[:200]}")


def scanner() -> Scanner:
    s = config.settings
    if s.scanner == "clamav":
        return ClamAVScanner(s.clamd_host, s.clamd_port, s.clamd_timeout_s)
    return BuiltinScanner()


def scan(filename: str, data: bytes) -> ScanResult:
    """Scan, turning an unreachable scanner into a Pending verdict (the worker scans it later)."""
    sc = scanner()
    try:
        return sc.scan(filename, data)
    except ScannerUnavailable as e:
        return ScanResult("Pending", sc.engine, str(e)[:200])


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
@dataclass
class Stored:
    ref: str
    encryption: str | None = None
    retain_until: datetime | None = None


class Store(Protocol):
    backend: str
    def put(self, key: str, data: bytes) -> Stored: ...
    def get(self, ref: str) -> bytes: ...
    def set_legal_hold(self, ref: str, on: bool) -> None: ...
    def mark_quarantined(self, ref: str, detail: str) -> None: ...


class LocalStore:
    backend = "local"

    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _path(self, ref: str) -> Path:
        p = (self.root / ref).resolve()
        if self.root not in p.parents:
            raise EvidenceRejected("Invalid storage reference")
        return p

    def put(self, key: str, data: bytes) -> Stored:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)          # atomic: never a half-written evidence file
        return Stored(key)

    def get(self, ref: str) -> bytes:
        return self._path(ref).read_bytes()

    def set_legal_hold(self, ref: str, on: bool) -> None:
        pass                        # recorded in the database; a local disk has no hold mechanism

    def mark_quarantined(self, ref: str, detail: str) -> None:
        pass


class S3Store:
    backend = "s3"

    def __init__(self, client, bucket: str, prefix: str = ""):
        self.c, self.bucket, self.prefix = client, bucket, prefix

    def put(self, key: str, data: bytes) -> Stored:
        s = config.settings
        ref = f"{self.prefix}{key}"
        args = {"Bucket": self.bucket, "Key": ref, "Body": data, "ContentType": "application/octet-stream",
                # Object Lock writes must carry an integrity checksum; the store rejects a body that doesn't match.
                "ChecksumAlgorithm": "SHA256",
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
                "ContentMD5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode(),
                "ServerSideEncryption": s.evidence_s3_sse}
        if s.evidence_s3_sse == "aws:kms" and s.evidence_s3_kms_key_id:
            args["SSEKMSKeyId"] = s.evidence_s3_kms_key_id
        retain = None
        if s.evidence_retention_days > 0:
            retain = (datetime.now(timezone.utc) + timedelta(days=s.evidence_retention_days)).replace(microsecond=0)
            args.update(ObjectLockMode=s.evidence_retention_mode, ObjectLockRetainUntilDate=retain)
        r = self.c.put_object(**args)
        return Stored(ref, r.get("ServerSideEncryption") or s.evidence_s3_sse, retain)

    def get(self, ref: str) -> bytes:
        try:
            return self.c.get_object(Bucket=self.bucket, Key=ref)["Body"].read()
        except self.c.exceptions.NoSuchKey as e:
            raise FileNotFoundError(ref) from e

    def set_legal_hold(self, ref: str, on: bool) -> None:
        if config.settings.evidence_retention_days > 0:      # legal holds need an Object Lock bucket
            self.c.put_object_legal_hold(Bucket=self.bucket, Key=ref, LegalHold={"Status": "ON" if on else "OFF"})

    def mark_quarantined(self, ref: str, detail: str) -> None:
        # The object stays (a retention lock forbids deleting it) but is tagged; ResilienceAI never serves it.
        self.c.put_object_tagging(Bucket=self.bucket, Key=ref, Tagging={"TagSet": [
            {"Key": "resilienceai-quarantine", "Value": "infected"},
            {"Key": "resilienceai-signature", "Value": re.sub(r"[^\w.\-+ ]", "_", detail)[:250] or "malware"}]})

    def check(self) -> list[str]:
        """Problems with the bucket's setup (used by /ready and at worker start)."""
        problems = []
        try:
            self.c.head_bucket(Bucket=self.bucket)
        except Exception as e:  # noqa: BLE001
            return [f"Evidence bucket {self.bucket} not reachable: {type(e).__name__}"]
        if config.settings.evidence_retention_days > 0:
            try:
                lock = self.c.get_object_lock_configuration(Bucket=self.bucket)["ObjectLockConfiguration"]
                if lock.get("ObjectLockEnabled") != "Enabled":
                    problems.append("Object Lock is not enabled on the evidence bucket")
            except Exception:  # noqa: BLE001
                problems.append("Object Lock is not enabled on the evidence bucket")
        return problems


@lru_cache(maxsize=4)
def _s3_client(endpoint: str, region: str):
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=endpoint or None, region_name=region,
                        config=Config(retries={"max_attempts": 5, "mode": "standard"},
                                      s3={"addressing_style": "path" if endpoint else "auto"}))


def store() -> Store:
    s = config.settings
    if s.evidence_store == "local":
        return LocalStore(s.evidence_dir)
    if s.evidence_store == "s3":
        return S3Store(_s3_client(s.evidence_s3_endpoint, s.evidence_s3_region), s.evidence_s3_bucket, s.evidence_s3_prefix)
    raise RuntimeError(f"Evidence store '{s.evidence_store}' is not configured in this build")


def store_for(backend: str) -> Store:
    """The store a file was written to (a deployment can move from local to s3; older files stay readable)."""
    current = store()
    if current.backend == backend:
        return current
    if backend == "local":
        return LocalStore(config.settings.evidence_dir)
    raise RuntimeError(f"Evidence stored in '{backend}' but that store isn't configured")


def object_key(tenant_id: UUID, event_id: UUID) -> str:
    return f"{tenant_id}/{event_id}/{uuid4()}"
