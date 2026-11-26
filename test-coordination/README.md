# ResilienceAI — Test Programme & Coordination Centre (backend, increment 1)

FastAPI + PostgreSQL service implementing the coordination-first MVP domain: programmes, templates, test events, stakeholders, activities, dependencies, readiness and go/no-go, live coordination, evidence, findings, measurements, approvals, closure and audit.

**Boundary (spec §1):** the service records and governs tests. It never executes failover or infrastructure commands, and no AI component changes lifecycle state.

## Run it

```bash
pip install -r requirements-dev.txt   # runtime + pytest and ruff (the image installs requirements.txt only)
# PostgreSQL 15+ with two roles: an owner (runs migrations) and app_user (no table ownership, so RLS applies)
createuser app_user --pwprompt        # password "app" matches the default DATABASE_URL
createdb resilience
export WORKER_DATABASE_URL=postgresql+psycopg://postgres@localhost/resilience
export DATABASE_URL=postgresql+psycopg://app_user:app@localhost/resilience
python -m scripts.migrate             # applies pending migrations/*.sql; --status lists them
python -m scripts.seed --reset        # wipes, migrates and seeds tenant BANK with a user per role
python -m scripts.demo                # optional: reset and load a demo portfolio (tests at every stage)
uvicorn app.main:app --reload         # OpenAPI docs at http://localhost:8000/docs
```

Dev authentication (`AUTH_MODE=dev`, the default) uses the headers `X-Tenant-Id: <printed by seed>` and `X-User-Subject: arjun.pm`. Seeded subjects: `arjun.pm` (programme manager), `meera.tm` (test manager), `vikram.owner`, `rahul.dba`, `farah.biz` (business owner), `rohan.risk`, `leela.cio`, `ext.vendor`, `audit.ia`, and others; see `scripts/seed.py`. **Production uses `AUTH_MODE=oidc`** (Microsoft Entra ID sign-in; setup in [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md#microsoft-entra-id-sign-in), admin commands in `scripts/entra.py`).

An existing database created before `scripts.migrate` existed needs `python -m scripts.migrate --baseline` once (or rerun `scripts.demo`). Deployment with Docker: [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md).

## Tests

```bash
pytest -q        # 74 tests: domain units + end-to-end against a real PostgreSQL (schema is reset)
```

`tests/test_journey.py::test_core_banking_journey` walks the spec §31 demo end to end: programme → template → event → plan approval → stakeholders and acknowledgements → blocked DB prerequisite → readiness gate refuses go → evidence reject/resubmit → readiness exception → go decision → live (announcements, pause/resume) → RTO/RPO capture → findings → closure checklist → sign-offs → frozen closure report → audit-chain verification. Other tests cover QA-04/05/07/13/14/15 and transaction rollback.

## Layout

```
migrations/001_test_coordination.sql   schema source of truth: 30 tables, CHECKs, RLS, append-only audit, grants
app/domain/        pure rules: lifecycle, dependency graph, readiness gate, measurements, activity workflow, audit chain
app/services/      transactional use cases: events, people, work, views, notifications (outbox), audit, idempotency
app/api/routes.py  REST endpoints (Appendix A plus what the ten screens need)
app/authz.py       RBAC, event-level access, approval policy, segregation of duties
scripts/seed.py    tenant provisioning and the Core Banking DR template
docs/DESIGN_NOTES.md   spec review: gaps found and decisions taken
```

## Key mechanisms

- **Tenant isolation:** each request runs in one transaction with `app.tenant_id` set, and PostgreSQL RLS (`FORCE`) filters every tenant table. App-level checks sit on top.
- **Optimistic locking:** `expected_version` on event, activity and readiness updates; a mismatch returns 409.
- **Idempotency:** `Idempotency-Key` header on event creation, evidence and approvals. Notifications dedupe by business key.
- **Outbox:** communications and per-recipient/per-channel jobs are written in the business transaction. `notifications.dispatch_all_tenants()` delivers with exponential backoff and dead-letters after 5 attempts. Channels use `SimulatedAdapter` until the Graph and SMTP adapters are registered in `ADAPTERS`.
- **Escalations:** `people.run_escalations(tenant_id)` handles acknowledgement-SLA breaches and overdue activities. It is idempotent, so it is safe to schedule (Temporal or cron).

## Assistant (AI, first step: RA-069 to RA-072)

Four features, each grounded in the test's own records and logged in `ai_activity_records` with its sources:

| Feature | Where | Who |
|---|---|---|
| Blocker briefing: what holds up the go, and who should do what next | Readiness tab | Internal participants |
| Readiness forecast: projected readiness date, what drives it, the rules used | Readiness tab | Internal participants |
| Status summary: short executive update; every 30 min automatically while live | Timeline tab | Internal participants |
| Message drafts: reminder, escalation, readiness update, go request | Timeline tab | Test manager, owner, programme manager |

Rules that always hold:
- The assistant only reads. It never approves, starts, closes or fails over anything, and the readiness gate stays rule-based. A draft is sent only when a manager presses Send, in their name.
- Every suggestion is stored with the records it used and what a person did with it (helpful, edited, sent, dismissed with a reason). Programme, risk, audit and admin roles see this in **Reports → Assistant oversight**.
- External users (vendors) don't get the assistant.
- `AI_PROVIDER=rules` (default) writes everything from the records with no external service. `AI_PROVIDER=anthropic` with `ANTHROPIC_API_KEY` lets Claude (`AI_MODEL`) reword that text: names are replaced with placeholders before sending, the model may use only the facts given, and its answer is checked (no unknown record codes or people, sensible length). Any failure falls back to the built-in text, and the reason is logged.

Scheduled jobs (notification delivery, escalations, live summaries): `python -m scripts.worker` (every minute) or `--once` from cron.

## Reports

- `GET /test-events/{id}/closure-report.pdf`: closure report (A4 PDF). A closed test's report is rendered from the snapshot frozen at closure; before that it is marked DRAFT and lists what is outstanding.
- `GET /reports/programme-status.xlsx?programme_id=&tz=`: programme workbook with Summary, Tests, Exceptions, Findings, Decisions and Schedule changes sheets. For portfolio roles only.

Both are built with pure-Python libraries (`reportlab`, `openpyxl`) and every download is written to the audit trail.

## Evidence files

Uploads go to `EVIDENCE_STORE` (`local` by default: files under `EVIDENCE_DIR`, which defaults to `test-coordination/evidence-store/`). The limit is `EVIDENCE_MAX_MB` (25). Each file's SHA-256 is computed in the browser and on the server, stored with the metadata, and re-checked on every download; a mismatch is refused and audited. Files are always served as attachments with `nosniff`. The built-in scanner only recognises the EICAR test file: connect the bank's scanner (ClamAV/ICAP) and an encrypted object store (e.g. S3 with SSE-KMS) for production through `app/services/storage.py`.

## Backlog coverage

Built (API and tests): RA-001–004, 007, 009–011, 013, 014, 016–019, 021, 022, 024–028, 030–035, 037–039, 041–044 (simulated delivery), 047, 048, 050–055, 057–064.

Not yet built: the frontend (calendar, workspace and control-room screens), SSO/OIDC verifier (RA-005/006), recurring events (RA-015), real Outlook/Teams adapters (RA-045/046), presigned upload + malware scan for evidence files, connectors (RA-065–068), AI assistance (RA-069–072), real-time push (WebSocket/SSE for RA-051/056), load tests.
