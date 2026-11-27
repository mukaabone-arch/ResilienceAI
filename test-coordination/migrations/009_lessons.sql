-- ResilienceAI — Test Coordination, migration 009: cross-test learning
-- Lessons are worked out from earlier, related tests whenever a test is opened (nothing is copied). What planners do
-- with each lesson is kept here: turned into a readiness condition of this test, or set aside with a reason.
BEGIN;

CREATE TABLE test_lesson_decisions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id),
  event_id           uuid NOT NULL REFERENCES test_events(id),
  lesson_key         varchar(64) NOT NULL,             -- stable key of the lesson (kind + what it is about)
  decision           varchar(20) NOT NULL CHECK (decision IN ('Applied', 'Dismissed')),
  readiness_item_id  uuid REFERENCES test_readiness_items(id),
  reason             text,
  lesson             jsonb NOT NULL,                   -- the lesson as it was shown when the decision was made
  decided_by         uuid NOT NULL REFERENCES users(id),
  decided_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, event_id, lesson_key),
  CHECK ((decision = 'Applied') = (readiness_item_id IS NOT NULL)),
  CHECK (decision <> 'Dismissed' OR reason IS NOT NULL)
);
CREATE INDEX ix_lesson_item ON test_lesson_decisions (tenant_id, readiness_item_id) WHERE readiness_item_id IS NOT NULL;

ALTER TABLE test_lesson_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_lesson_decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON test_lesson_decisions USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    -- A decision can be reversed (deleted) while the test is still being planned; every change is in the audit trail.
    GRANT SELECT, INSERT, DELETE ON test_lesson_decisions TO app_user;
  END IF;
END $$;

COMMIT;
