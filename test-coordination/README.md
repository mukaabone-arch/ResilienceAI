# ResilienceAI — Test Programme & Coordination Centre (backend, increment 1)

FastAPI + PostgreSQL service implementing the coordination-first MVP domain: programmes, templates, test events, stakeholders, activities, dependencies, readiness and go/no-go, live coordination, evidence, findings, measurements, approvals, closure and audit.

**Boundary (spec §1):** the service records and governs tests. It never executes failover or infrastructure commands, and no AI component changes lifecycle state.

## Run it

```bash
pip install -r requirements.txt
# PostgreSQL 15+ with two roles: an owner (runs migrations) and app_user (no table ownership, so RLS applies)
createuser app_user --pwprompt        # password "app" matches the default DATABASE_URL
createdb resilience
export WORKER_DATABASE_URL=postgresql+psycopg://postgres@localhost/resilience
export DATABASE_URL=postgresql+psycopg://app_user:app@localhost/resilience
python -m scripts.seed --reset        # applies migrations/*.sql and seeds tenant BANK with a user per role
uvicorn app.main:app --reload         # OpenAPI docs at http://localhost:8000/docs
```

Dev authentication (`AUTH_MODE=dev`, the default) uses the headers `X-Tenant-Id: <printed by seed>` and `X-User-Subject: arjun.pm`. Seeded subjects: `arjun.pm` (programme manager), `meera.tm` (test manager), `vikram.owner`, `rahul.dba`, `farah.biz` (business owner), `rohan.risk`, `leela.cio`, `ext.vendor`, `audit.ia`, and others; see `scripts/seed.py`. **Production must use `AUTH_MODE=oidc`** with `verify_oidc_token` wired to Entra ID.

## Tests

```bash
pytest -q        # 18 tests: domain units + end-to-end against a real PostgreSQL (schema is reset)
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

## Backlog coverage

Built (API and tests): RA-001–004, 007, 009–011, 013, 014, 016–019, 021, 022, 024–028, 030–035, 037–039, 041–044 (simulated delivery), 047, 048, 050–055, 057–064.

Not yet built: the frontend (calendar, workspace and control-room screens), SSO/OIDC verifier (RA-005/006), recurring events (RA-015), real Outlook/Teams adapters (RA-045/046), presigned upload + malware scan for evidence files, connectors (RA-065–068), AI assistance (RA-069–072), real-time push (WebSocket/SSE for RA-051/056), load tests.
