-- ResilienceAI — Test Coordination, migration 002: recurring test series (RA-015)
BEGIN;

CREATE TABLE test_event_series (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  title         varchar(250) NOT NULL,
  frequency     varchar(20) NOT NULL CHECK (frequency IN ('weekly','monthly','quarterly','half_yearly','yearly')),
  occurrences   integer NOT NULL CHECK (occurrences BETWEEN 2 AND 52),
  created_by    uuid NOT NULL REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE test_events
  ADD COLUMN series_id    uuid REFERENCES test_event_series(id),
  ADD COLUMN series_index integer,
  ADD CONSTRAINT test_events_series_pair CHECK ((series_id IS NULL) = (series_index IS NULL));
CREATE INDEX ix_events_series ON test_events (series_id, series_index) WHERE series_id IS NOT NULL;

ALTER TABLE test_event_series ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_event_series FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON test_event_series USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON test_event_series TO app_user;
  END IF;
END $$;

COMMIT;
