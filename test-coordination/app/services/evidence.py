"""Evidence files: upload, integrity-checked download, verification and the evidence register (Screen 09)."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update

from .. import authz
from ..db import t
from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from ..security import Ctx
from . import audit, storage
from .common import child_of_event, event, now
from .work import _is_activity_owner, submit_evidence


def upload(ctx: Ctx, *, activity_id: UUID | None, readiness_item_id: UUID | None, filename: str,
           data: bytes, content_type: str | None, evidence_type: str, client_sha256: str | None,
           supersedes_id: UUID | None) -> dict:
    table, target_id = ("test_activities", activity_id) if activity_id else ("test_readiness_items", readiness_item_id)
    _, ev = child_of_event(ctx, table, target_id)     # permission and existence checks happen in submit_evidence too
    name = storage.safe_filename(filename)
    try:
        storage.check_file(name, data)
    except storage.EvidenceRejected as e:
        raise Unprocessable(str(e)) from e
    digest = storage.sha256(data)
    if client_sha256 and client_sha256.lower() != digest:
        raise Unprocessable("The file changed between your browser and the server (checksums differ). Please upload it again.")
    verdict = storage.scanner.scan(name, data)
    if verdict == "Infected":
        audit.record(ctx, "evidence.rejected_malware", "Event", ev["id"], event_id=ev["id"],
                     new={"filename": name, "sha256": digest})
        raise Unprocessable("The file was flagged by the malware scan and was not stored.")
    store = storage.store()
    ref = store.put(storage.object_key(ctx.me.tenant_id, ev["id"]), data)
    return submit_evidence(ctx, activity_id=activity_id, readiness_item_id=readiness_item_id, data={
        "evidence_type": evidence_type, "storage_reference": ref, "filename": name, "checksum": digest,
        "supersedes_id": supersedes_id, "storage_backend": store.backend, "size_bytes": len(data),
        "content_type": (content_type or "application/octet-stream")[:120], "scan_status": verdict,
    })


def _can_read_file(ctx: Ctx, item: dict, ev: dict) -> None:
    authz.require_view(ctx, ev)
    if ctx.me.is_external and item["submitted_by"] != ctx.me.user_id:
        act = item["activity_id"] and dict(ctx.conn.execute(select(t("test_activities")).where(
            t("test_activities").c.id == item["activity_id"])).mappings().first() or {})
        if not (act and _is_activity_owner(ctx, act)):
            raise Forbidden("Vendors can open only evidence for their own activities")


def _load_bytes(item: dict) -> bytes:
    if item["storage_backend"] == "link":
        raise Conflict("This evidence is a link to an external record, not an uploaded file")
    try:
        return storage.store().get(item["storage_reference"])
    except FileNotFoundError as e:
        raise NotFound("The stored file is missing") from e


def verify(ctx: Ctx, evidence_id: UUID, *, record: bool = True) -> dict:
    item, ev = child_of_event(ctx, "test_evidence", evidence_id)
    _can_read_file(ctx, item, ev)
    if item["storage_backend"] == "link":
        return {"evidence_id": evidence_id, "applicable": False, "valid": None}
    try:
        actual = storage.sha256(_load_bytes(item))
        ok = actual == item["checksum"]
    except NotFound:
        actual, ok = None, False
    if record:
        evd = t("test_evidence")
        if ok:
            ctx.conn.execute(update(evd).where(evd.c.id == evidence_id).values(last_verified_at=now()))
        audit.record(ctx, "evidence.verified" if ok else "evidence.integrity_failed", "Evidence", evidence_id,
                     event_id=ev["id"], new={"recorded_sha256": item["checksum"], "actual_sha256": actual})
    return {"evidence_id": evidence_id, "applicable": True, "valid": ok, "recorded_sha256": item["checksum"],
            "actual_sha256": actual, "checked_at": now()}


def download(ctx: Ctx, evidence_id: UUID) -> tuple[bytes | None, dict, dict | None]:
    """Returns (bytes, item, None) when the file is intact, or (None, item, failure) when it is not.

    A failed integrity check is reported as a normal return, not an exception, so the request's
    transaction still commits and the audit record of the failure is kept.
    """
    item, ev = child_of_event(ctx, "test_evidence", evidence_id)
    _can_read_file(ctx, item, ev)
    data = _load_bytes(item)
    actual = storage.sha256(data)
    if actual != item["checksum"]:
        audit.record(ctx, "evidence.integrity_failed", "Evidence", evidence_id, event_id=ev["id"],
                     new={"recorded_sha256": item["checksum"], "actual_sha256": actual, "on": "download"})
        return None, item, {"error": "Integrity check failed: the stored file no longer matches the checksum recorded "
                                     "at upload. It has not been served.",
                            "details": [f"Recorded {item['checksum']}", f"Found {actual}"]}
    audit.record(ctx, "evidence.downloaded", "Evidence", evidence_id, event_id=ev["id"], new={"sha256": actual})
    return data, item, None


def register(ctx: Ctx, event_id: UUID) -> list[dict]:
    ev = event(ctx, event_id)
    evd, a, r, u = t("test_evidence"), t("test_activities"), t("test_readiness_items"), t("users")
    sub, rev = u.alias("sub"), u.alias("rev")
    rows = [dict(x) for x in ctx.conn.execute(
        select(evd, a.c.activity_code, a.c.title.label("activity_title"), a.c.reviewer_user_id,
               r.c.title.label("readiness_title"), sub.c.display_name.label("submitted_by_name"),
               rev.c.display_name.label("reviewed_by_name"))
        .outerjoin(a, a.c.id == evd.c.activity_id).outerjoin(r, r.c.id == evd.c.readiness_item_id)
        .join(sub, sub.c.id == evd.c.submitted_by).outerjoin(rev, rev.c.id == evd.c.reviewed_by)
        .where(evd.c.event_id == event_id).order_by(evd.c.submitted_at.desc())).mappings()]
    if ctx.me.is_external:
        rows = [x for x in rows if x["submitted_by"] == ctx.me.user_id]
    superseded = {x["supersedes_id"] for x in rows if x["supersedes_id"]}
    for x in rows:
        x["superseded"] = x["id"] in superseded
        x["can_review"] = (x["review_status"] == "Pending" and x["submitted_by"] != ctx.me.user_id and (
            authz.can_manage_event(ctx, ev) or ctx.me.has(authz.Role.RISK) or x["reviewer_user_id"] == ctx.me.user_id))

    return rows
