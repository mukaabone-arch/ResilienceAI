"""Evidence files: upload, integrity-checked download, verification and the evidence register (Screen 09)."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update

from .. import authz
from ..authz import Role
from ..db import t
from ..errors import Conflict, Forbidden, NotFound, Unprocessable
from ..security import Ctx
from . import audit, storage
from .common import child_of_event, event, now, row
from .work import _is_activity_owner, check_can_submit_evidence, submit_evidence


def upload(ctx: Ctx, *, activity_id: UUID | None, readiness_item_id: UUID | None, filename: str,
           data: bytes, content_type: str | None, evidence_type: str, client_sha256: str | None,
           supersedes_id: UUID | None) -> dict:
    # All submission rules first: once a file is in a retention-locked store it can't be removed.
    ev = check_can_submit_evidence(ctx, activity_id=activity_id, readiness_item_id=readiness_item_id,
                                   supersedes_id=supersedes_id)
    name = storage.safe_filename(filename)
    try:
        storage.check_file(name, data)
    except storage.EvidenceRejected as e:
        raise Unprocessable(str(e)) from e
    digest = storage.sha256(data)
    if client_sha256 and client_sha256.lower() != digest:
        raise Unprocessable("The file changed between your browser and the server (checksums differ). Please upload it again.")
    verdict = storage.scan(name, data)
    if verdict.status == "Infected":
        audit.record(ctx, "evidence.rejected_malware", "Event", ev["id"], event_id=ev["id"],
                     new={"filename": name, "sha256": digest, "signature": verdict.detail, "engine": verdict.engine})
        raise Unprocessable(f"The file was flagged by the malware scan ({verdict.detail}) and was not stored.")
    store = storage.store()
    saved = store.put(storage.object_key(ctx.me.tenant_id, ev["id"]), data)
    return submit_evidence(ctx, activity_id=activity_id, readiness_item_id=readiness_item_id, data={
        "evidence_type": evidence_type, "storage_reference": saved.ref, "filename": name, "checksum": digest,
        "supersedes_id": supersedes_id, "storage_backend": store.backend, "size_bytes": len(data),
        "content_type": (content_type or "application/octet-stream")[:120], "scan_status": verdict.status,
        "scan_engine": verdict.engine, "scan_detail": verdict.detail if verdict.status == "Pending" else None,
        "scanned_at": now() if verdict.status in ("Clean", "NotScanned") else None,
        "encryption": saved.encryption, "retain_until": saved.retain_until,
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
        return storage.store_for(item["storage_backend"]).get(item["storage_reference"])
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
    if item["scan_status"] == "Pending":
        raise Conflict("This file is waiting for its malware scan. It can be opened once the scan finds it clean.")
    if item["scan_status"] == "Infected":
        audit.record(ctx, "evidence.download_blocked", "Evidence", evidence_id, event_id=ev["id"],
                     new={"reason": "quarantined", "signature": item["scan_detail"]})
        return None, item, {"error": f"This file was quarantined by the malware scan ({item['scan_detail']}). "
                                     "It can't be opened.", "details": []}
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
        x["can_review"] = (x["review_status"] == "Pending" and x["submitted_by"] != ctx.me.user_id
                           and x["scan_status"] not in ("Pending", "Infected") and (
            authz.can_manage_event(ctx, ev) or ctx.me.has(authz.Role.RISK) or x["reviewer_user_id"] == ctx.me.user_id))

    hold_ok = ctx.me.has(*LEGAL_HOLD_ROLES)
    for x in rows:
        x["can_legal_hold"] = hold_ok and x["storage_backend"] != "link"
    return rows


LEGAL_HOLD_ROLES = (Role.RISK, Role.AUDITOR, Role.ADMIN)


def set_legal_hold(ctx: Ctx, evidence_id: UUID, on: bool, reason: str | None) -> dict:
    """Keep a file beyond its retention date (an investigation, a dispute, a regulator's request)."""
    item, ev = child_of_event(ctx, "test_evidence", evidence_id, for_update=True)
    authz.require_view(ctx, ev)
    if not ctx.me.has(*LEGAL_HOLD_ROLES):
        raise Forbidden("Only risk and compliance, audit or the programme administrator can change a legal hold")
    if item["storage_backend"] == "link":
        raise Conflict("A legal hold applies to uploaded files; this evidence is a link to an external record")
    if on and not (reason or "").strip():
        raise Unprocessable("Give the reason for the legal hold, e.g. the investigation or request it relates to")
    if bool(item["legal_hold"]) == on:
        raise Conflict("The file is already on legal hold" if on else "The file isn't on legal hold")
    storage.store_for(item["storage_backend"]).set_legal_hold(item["storage_reference"], on)
    evd = t("test_evidence")
    new = dict(ctx.conn.execute(update(evd).where(evd.c.id == evidence_id).values(
        legal_hold=on, legal_hold_reason=reason.strip() if on else None,
        legal_hold_by=ctx.me.user_id if on else None, legal_hold_at=now() if on else None).returning(evd)).mappings().one())
    audit.record(ctx, "evidence.legal_hold_placed" if on else "evidence.legal_hold_released", "Evidence", evidence_id,
                 event_id=ev["id"], new={"reason": reason, "storage_reference": item["storage_reference"]})
    return new


def rescan_pending(tenant_id: UUID, limit: int = 50) -> dict:
    """Worker job: scan files stored while the scanner was unreachable. Clean files become available;
    infected ones are quarantined and the submitter and test manager are told."""
    from sqlalchemy import and_

    from . import notifications
    from .people import _system_ctx
    from ..db import tenant_transaction
    out = {"clean": 0, "infected": 0, "still_pending": 0}
    with tenant_transaction(tenant_id) as conn:
        ctx = _system_ctx(conn, tenant_id)
        evd = t("test_evidence")
        items = [dict(r) for r in conn.execute(select(evd).where(and_(evd.c.scan_status == "Pending"))
                                               .order_by(evd.c.submitted_at).limit(limit).with_for_update(skip_locked=True)).mappings()]
        for item in items:
            try:
                data = storage.store_for(item["storage_backend"]).get(item["storage_reference"])
            except FileNotFoundError:
                conn.execute(update(evd).where(evd.c.id == item["id"]).values(
                    scan_status="Error", scan_detail="stored file missing", scan_attempts=item["scan_attempts"] + 1))
                continue
            result = storage.scan(item["filename"] or "evidence", data)
            values = {"scan_attempts": item["scan_attempts"] + 1, "scan_engine": result.engine}
            if result.status == "Pending":
                conn.execute(update(evd).where(evd.c.id == item["id"]).values(**values, scan_detail=result.detail))
                out["still_pending"] += 1
                continue
            conn.execute(update(evd).where(evd.c.id == item["id"]).values(
                **values, scan_status=result.status, scan_detail=result.detail, scanned_at=now()))
            ev = row(ctx, "test_events", item["event_id"])     # system job: no viewer to check
            if result.status == "Infected":
                storage.store_for(item["storage_backend"]).mark_quarantined(item["storage_reference"], result.detail or "")
                audit.record(ctx, "evidence.quarantined", "Evidence", item["id"], event_id=ev["id"], actor=None,
                             new={"signature": result.detail, "engine": result.engine, "sha256": item["checksum"]})
                notifications.enqueue(ctx, "critical_issue", event=ev, activity_id=item["activity_id"],
                                      recipients=[item["submitted_by"], ev["test_manager_user_id"]], actor=None,
                                      subject=f"[{ev['event_code']}] Evidence {item['evidence_code']} quarantined by the malware scan",
                                      body=f"{item['filename']} matched {result.detail}. It has been quarantined and can't be "
                                           "opened or accepted. Please upload a clean copy.",
                                      dedupe=f"quarantine:{item['id']}")
                out["infected"] += 1
            else:
                audit.record(ctx, "evidence.scanned", "Evidence", item["id"], event_id=ev["id"], actor=None,
                             new={"scan_status": result.status, "engine": result.engine})
                out["clean"] += 1
    return out
