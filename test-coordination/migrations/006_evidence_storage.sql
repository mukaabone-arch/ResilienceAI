-- ResilienceAI — Test Coordination, migration 006: secure evidence storage (roadmap R1.0)
-- Files live in encrypted object storage under a retention lock; every file is malware-scanned before anyone can
-- open or accept it; risk, audit and admin roles can place a legal hold.
BEGIN;

ALTER TABLE test_evidence DROP CONSTRAINT IF EXISTS test_evidence_scan_status_check;
ALTER TABLE test_evidence ADD CONSTRAINT test_evidence_scan_status_check
  CHECK (scan_status IN ('NotScanned', 'Pending', 'Clean', 'Infected', 'Error'));

ALTER TABLE test_evidence
  ADD COLUMN scanned_at        timestamptz,
  ADD COLUMN scan_engine       varchar(40),
  ADD COLUMN scan_detail       varchar(200),         -- the signature name when a file is infected
  ADD COLUMN scan_attempts     integer NOT NULL DEFAULT 0,
  ADD COLUMN encryption        varchar(40),          -- what the object store reported, e.g. aws:kms or AES256
  ADD COLUMN retain_until      timestamptz,          -- object-lock retention: no one can delete or overwrite before this
  ADD COLUMN legal_hold        boolean NOT NULL DEFAULT false,
  ADD COLUMN legal_hold_reason text,
  ADD COLUMN legal_hold_by     uuid REFERENCES users(id),
  ADD COLUMN legal_hold_at     timestamptz,
  ADD CONSTRAINT test_evidence_legal_hold_reason CHECK (NOT legal_hold OR legal_hold_reason IS NOT NULL);

CREATE INDEX test_evidence_scan_pending ON test_evidence (tenant_id) WHERE scan_status = 'Pending';

COMMIT;
