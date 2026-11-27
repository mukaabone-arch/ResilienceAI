-- ResilienceAI — Test Coordination, migration 007: go/no-go decision records (decision record, dissent, replay, audit pack)
-- A decision record is sealed when the decision is made: a snapshot of everything the decision rested on, a SHA-256
-- fingerprint of it, and the position in the tenant's hash-chained audit trail where the fingerprint was written.
-- Dissent is append-only: no one can edit or remove it.
BEGIN;

CREATE TABLE test_decision_records (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenants(id),
  event_id         uuid NOT NULL REFERENCES test_events(id),
  approval_id      uuid NOT NULL UNIQUE REFERENCES test_approvals(id),
  approval_type    varchar(50) NOT NULL,
  decision         varchar(30) NOT NULL,
  decided_by       uuid NOT NULL REFERENCES users(id),
  decided_at       timestamptz NOT NULL,
  present_user_ids uuid[] NOT NULL DEFAULT '{}',
  snapshot         jsonb NOT NULL,
  content_sha256   char(64) NOT NULL,
  audit_seq        bigint,
  audit_row_hash   char(64)
);
CREATE INDEX test_decision_records_event ON test_decision_records (event_id, decided_at);

CREATE TABLE test_decision_dissents (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id),
  decision_record_id uuid NOT NULL REFERENCES test_decision_records(id),
  user_id            uuid NOT NULL REFERENCES users(id),     -- whose view it is
  recorded_by        uuid NOT NULL REFERENCES users(id),     -- who wrote it down (the approver, during the call)
  kind               varchar(20) NOT NULL CHECK (kind IN ('Dissent', 'Concern')),
  reason             text NOT NULL CHECK (length(trim(reason)) > 0),
  during_decision    boolean NOT NULL,                       -- raised in the call, or added afterwards
  raised_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX test_decision_dissents_record ON test_decision_dissents (decision_record_id, raised_at);

ALTER TABLE test_decision_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_decision_records FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON test_decision_records USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());
ALTER TABLE test_decision_dissents ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_decision_dissents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON test_decision_dissents USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    -- Records are written once (the audit position is filled in the same transaction); dissent is insert-only.
    GRANT SELECT, INSERT ON test_decision_records TO app_user;
    GRANT UPDATE (audit_seq, audit_row_hash) ON test_decision_records TO app_user;
    GRANT SELECT, INSERT ON test_decision_dissents TO app_user;
  END IF;
END $$;

COMMIT;
