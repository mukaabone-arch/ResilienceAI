-- ResilienceAI — Test Coordination, migration 012: runbook automation (Ansible Automation Platform)
-- ResilienceAI still never runs commands itself. An activity can be linked to a job template in the bank's Ansible
-- Automation Platform (AAP); a person approves each launch, AAP runs it under its own access controls, and
-- ResilienceAI follows the job: status into the activity, output into evidence, failures into blockers.
BEGIN;

CREATE TABLE automation_integrations (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(id),
  name                 varchar(120) NOT NULL,
  kind                 varchar(20) NOT NULL DEFAULT 'aap' CHECK (kind IN ('aap')),
  base_url             varchar(300) NOT NULL,
  token_env            varchar(80) NOT NULL,            -- name of the environment variable holding the AAP token (from the vault)
  webhook_secret_hash  char(64) NOT NULL,               -- sha256 of the secret AAP sends with job notifications
  user_id              uuid NOT NULL UNIQUE REFERENCES users(id),   -- the integration's own person record, for attribution
  created_at           timestamptz NOT NULL DEFAULT now(),
  disabled_at          timestamptz,
  UNIQUE (tenant_id, name)
);

CREATE TABLE activity_runbooks (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(id),
  activity_id          uuid NOT NULL UNIQUE REFERENCES test_activities(id),
  integration_id       uuid NOT NULL REFERENCES automation_integrations(id),
  job_template_id      integer NOT NULL,
  job_template_name    varchar(250) NOT NULL,
  extra_vars           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- fixed variables, set when linking; nobody adds more at launch
  launch_mode          varchar(20) NOT NULL DEFAULT 'approve' CHECK (launch_mode IN ('approve', 'auto')),
  timeout_minutes      integer NOT NULL DEFAULT 120 CHECK (timeout_minutes BETWEEN 1 AND 1440),
  linked_by            uuid NOT NULL REFERENCES users(id),
  linked_at            timestamptz NOT NULL DEFAULT now(),
  version              integer NOT NULL DEFAULT 1
);

CREATE TABLE automation_runs (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(id),
  event_id             uuid NOT NULL REFERENCES test_events(id),
  activity_id          uuid NOT NULL REFERENCES test_activities(id),
  runbook_id           uuid NOT NULL REFERENCES activity_runbooks(id),
  attempt              integer NOT NULL,
  job_template_id      integer NOT NULL,
  extra_vars           jsonb NOT NULL,                     -- exactly what was sent
  approved_by          uuid NOT NULL REFERENCES users(id), -- the person who pressed Run
  status               varchar(20) NOT NULL DEFAULT 'Launching'
                       CHECK (status IN ('Launching', 'Pending', 'Running', 'Successful', 'Failed', 'Error', 'Canceled', 'Lost')),
  external_job_id      integer,
  external_url         varchar(400),
  launched_at          timestamptz NOT NULL DEFAULT now(),
  started_at           timestamptz,
  finished_at          timestamptz,
  last_checked_at      timestamptz,
  failure_summary      text,
  evidence_id          uuid REFERENCES test_evidence(id),
  cancel_requested_by  uuid REFERENCES users(id),
  timeout_notified     boolean NOT NULL DEFAULT false,
  UNIQUE (activity_id, attempt)
);
CREATE INDEX ix_runs_active ON automation_runs (tenant_id, status) WHERE status IN ('Launching', 'Pending', 'Running');
CREATE UNIQUE INDEX ux_runs_job ON automation_runs (tenant_id, runbook_id, external_job_id) WHERE external_job_id IS NOT NULL;
-- One live run per activity at a time.
CREATE UNIQUE INDEX ux_runs_one_active ON automation_runs (activity_id) WHERE status IN ('Launching', 'Pending', 'Running');

DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['automation_integrations', 'activity_runbooks', 'automation_runs'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tbl);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
                   'WITH CHECK (tenant_id = current_tenant_id())', tbl);
  END LOOP;
END $$;

-- The AAP webhook arrives without a signed-in person; this answers only "which organisation owns this integration".
CREATE FUNCTION tenant_for_integration(p_id uuid) RETURNS uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
  AS $$ SELECT tenant_id FROM automation_integrations WHERE id = p_id AND disabled_at IS NULL $$;
REVOKE ALL ON FUNCTION tenant_for_integration(uuid) FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    -- Integrations are created by the operations script only.
    GRANT SELECT ON automation_integrations TO app_user;
    GRANT SELECT, INSERT, UPDATE, DELETE ON activity_runbooks TO app_user;
    GRANT SELECT, INSERT, UPDATE ON automation_runs TO app_user;
    GRANT EXECUTE ON FUNCTION tenant_for_integration(uuid) TO app_user;
  END IF;
END $$;

COMMIT;
