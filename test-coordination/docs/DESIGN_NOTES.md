# Specification review and design decisions

Review of *ResilienceAI — Banking Test Programme & Coordination Centre* (MVP baseline), with the decisions taken in this codebase. The spec holds up well overall: the coordination-only boundary, separating lifecycle from outcome, separating delivery from acknowledgement, and the append-only audit are the right calls. The points below are gaps or inconsistencies that would have caused rework if left open.

## A. Gaps resolved in code

| # | Spec issue | Decision implemented |
|---|---|---|
| 1 | `lifecycle_status` lists 7 states, but the §20 diagram uses `Readiness` and `Paused` too. `Cancelled` and `Rescheduled` are left undefined. | 10 states: Draft, Planned, Mobilising, Readiness, Ready, Live, Paused, Closure, Closed, Cancelled. **Rescheduling is an action, not a state.** It is allowed up to Ready; rescheduling a Ready event sends it back to Readiness. `app/domain/lifecycle.py` is the single definition. |
| 2 | Nothing says when a go decision stops being valid. | A Go decision is **superseded** when the event is rescheduled, when a mandatory readiness item regresses (the event automatically leaves Ready), or when the event returns to Mobilising. `start` re-checks the readiness gate as well as the Go decision. |
| 3 | "Policy-defined exceptions through authorised approval" has no data model. | A `ReadinessException` approval references one readiness item (`subject_id`), needs a justification and is limited to the Programme Manager and Risk roles. |
| 4 | `approval_type` lists only Go/No-go / Business / Closure, yet the workflow needs plan approval, resume-after-pause, risk review and blackout override. | Added `Plan`, `Resume`, `RiskReview`, `ReadinessException` and `BlackoutOverride`. `Plan` and `BlackoutOverride` are recorded by their workflow action, never posted directly. |
| 5 | The meaning of a `Conditional` decision is undefined. | Counts as a positive decision, and the conditions text is mandatory (DB CHECK). |
| 6 | Segregation of duties isn't stated, which audit will flag. | The test manager can't approve go/no-go, resume or closure for their own event. Critical events need the Programme Manager, not just the owner. Nobody can review their own evidence or activity, validate their own measurement, or accept their own remediation. |
| 7 | `test_communications` has one `channel` and one `delivery_status`, but a single communication goes to many recipients over several channels. | Delivery state lives in `notification_jobs`, one row per recipient × channel (the transactional outbox). Communications carry a `dedupe_key`, so retried requests never send twice. |
| 8 | Assignment acknowledgement is stored in two places (`test_stakeholders.acknowledged_at` and `test_acknowledgements`). | Both are updated by one service call. The stakeholder row is authoritative for the assignment. |
| 9 | Reassignment history is required, but no table is specified. | Added `activity_assignment_history`. Replaced stakeholders are deactivated, never deleted (partial unique index on active rows). |
| 10 | Closure must check "mandatory evidence", but nothing marks evidence as mandatory. | Added `evidence_required` on activities (as on readiness items). Submission is blocked without evidence, and closure is blocked without accepted evidence. |
| 11 | Measurements have one `unit` for both target and actual, and no comparison direction. | Added `target_unit`, normalised values (seconds or percentage) and a rule per type (RTO/RPO: lower is better; Availability: higher is better). A measurement stays **Not Validated** until someone other than the recorder validates it. |
| 12 | Audit immutability (the spec rightly says an editable table isn't enough). | A hash chain per tenant (`prev_hash`/`row_hash`), an append-only trigger that also stops the owner role, UPDATE/DELETE revoked from `app_user`, and a `/audit/verify` endpoint. **WORM export is still a deployment task.** |
| 13 | `users`, `teams`, `business_services` and blackout periods are referenced but never defined. | Defined. Identity key is `(tenant_id, idp_subject)`, never email. |
| 14 | `test_event_audit.event_id` can't be filled for programme- or template-level actions. | Made nullable. |
| 15 | Vendor visibility isn't detailed enough to implement. | Vendors only see events they are assigned to, only their own activities, no bank staff email addresses and no audit entries in the timeline. Hidden events return 404 rather than 403. |
| 16 | Closure order (§15 steps 1–8) isn't enforced anywhere. | Closure approval is refused until the outcome is recorded, Business (plus RiskReview for Critical/High) has signed off and every other checklist item is clear. Changing the outcome supersedes earlier sign-offs. |

## B. Delivery-plan observations (not code)

- **Sprints 2–3 (15 Dec – 11 Jan) fall over the holidays.** Expect roughly 30–40% less capacity, or move the start date.
- **Sprint 8 is overloaded.** It holds the AI epic (P1) together with security testing, UAT and release. I recommend moving RA-069..072 after the pilot and running security testing (RA-074/078) continuously from Sprint 4.
- **Microsoft Graph / Teams app consent** at banks often takes 4–8 weeks of security review. The spec's Sprint 1 mitigation is correct and should be a named owner's action, not a note. Until consent lands, the simulated adapter (already built) keeps the demo journey working.
- **`workflow_definitions` vs Temporal overlap.** For the MVP, keep lifecycle rules in code (as built: versioned, testable, auditable) and use Temporal only for timers (reminders, SLA escalations, retries). Configurable workflow definitions add a lot of test surface for little pilot value.
- **Target figures** (99.9% availability, 2 s loads, 100 concurrent users) are reasonable, but QA-02's 100+ events plus the portfolio queries should be load-tested early. The indexes are in place.

## C. Open decisions for the pilot customer

1. Approval policy per criticality (the defaults are in `app/authz.py::APPROVAL_POLICY`, version `default-v1`).
2. Acknowledgement SLA (default 48 h) and escalation target (team lead, falling back to the test manager).
3. Whether a Conditional go permits start without further action (currently yes, with the conditions logged).
4. Where evidence is stored (bank S3-compatible store or SharePoint) and the malware-scanning service.
5. Data residency answers from §22 (`tenants.data_residency` holds them).
