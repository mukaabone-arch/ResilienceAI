-- ResilienceAI — Test Coordination, migration 008: evidence pre-checks (roadmap R1.1, AI assists, people decide)
-- Each uploaded file is checked against the activity it was attached to (dates, recovery targets, environment,
-- reuse, sensitive data, relevance). The result is an AI suggestion like any other: kept for oversight, with what
-- the reviewer did with it. It never accepts or rejects evidence.
BEGIN;

ALTER TABLE ai_activity_records DROP CONSTRAINT IF EXISTS ai_activity_records_feature_check;
ALTER TABLE ai_activity_records ADD CONSTRAINT ai_activity_records_feature_check
  CHECK (feature IN ('blocker_briefing','slip_forecast','status_summary','message_draft','evidence_precheck'));

-- The record a suggestion is about, when it is about one record (the evidence item for a pre-check).
ALTER TABLE ai_activity_records ADD COLUMN subject_id uuid;
CREATE INDEX ix_ai_subject ON ai_activity_records (tenant_id, subject_id, created_at DESC) WHERE subject_id IS NOT NULL;

-- Recovery targets for the test, so evidence can be compared with them before any measurement is recorded.
ALTER TABLE test_events
  ADD COLUMN rto_target_minutes integer CHECK (rto_target_minutes IS NULL OR rto_target_minutes > 0),
  ADD COLUMN rpo_target_minutes integer CHECK (rpo_target_minutes IS NULL OR rpo_target_minutes >= 0);

-- Duplicate detection looks files up by checksum.
CREATE INDEX IF NOT EXISTS ix_evidence_checksum ON test_evidence (tenant_id, checksum) WHERE checksum IS NOT NULL;

COMMIT;
