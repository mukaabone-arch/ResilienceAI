-- ResilienceAI — Test Coordination, migration 013: governed access for the bank's own AI agents
-- An agent (built and run by the bank) gets its own identity, a named accountable person, scoped permissions and an
-- expiring key. It is assigned activities like any stakeholder; every call it makes is logged. It can never approve:
-- runs it asks for wait for its accountable person or the test manager.
BEGIN;

CREATE TABLE agent_identities (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  user_id             uuid NOT NULL UNIQUE REFERENCES users(id),   -- its person record ("DBA Agent (agent)")
  name                varchar(120) NOT NULL,
  purpose             text NOT NULL,
  accountable_user_id uuid NOT NULL REFERENCES users(id),          -- the human answerable for what it does
  scopes              text[] NOT NULL,
  key_hash            char(64) NOT NULL UNIQUE,                    -- sha256 of the key; the key is shown once
  key_hint            varchar(12) NOT NULL,                        -- last characters, to recognise it
  expires_at          timestamptz NOT NULL,
  created_by          varchar(120) NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  last_used_at        timestamptz,
  suspended_at        timestamptz,
  suspended_by        uuid REFERENCES users(id),
  suspended_reason    text,
  UNIQUE (tenant_id, name),
  CHECK (accountable_user_id <> user_id),
  CHECK (scopes <@ ARRAY['read','acknowledge','update_status','report_blocker','post_update','attach_evidence','request_run']::text[])
);

CREATE TABLE agent_actions (
  id           bigserial PRIMARY KEY,
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  agent_id     uuid NOT NULL REFERENCES agent_identities(id),
  at           timestamptz NOT NULL DEFAULT now(),
  method       varchar(8) NOT NULL,
  path         varchar(300) NOT NULL,
  status_code  integer NOT NULL,
  outcome      varchar(20) NOT NULL CHECK (outcome IN ('done', 'refused', 'failed')),
  detail       text
);
CREATE INDEX ix_agent_actions ON agent_actions (tenant_id, agent_id, at DESC);

CREATE TABLE automation_run_requests (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  event_id      uuid NOT NULL REFERENCES test_events(id),
  activity_id   uuid NOT NULL REFERENCES test_activities(id),
  agent_id      uuid NOT NULL REFERENCES agent_identities(id),
  reason        text NOT NULL,
  status        varchar(20) NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Approved', 'Declined', 'Expired')),
  requested_at  timestamptz NOT NULL DEFAULT now(),
  decided_by    uuid REFERENCES users(id),
  decided_at    timestamptz,
  decision_note text,
  run_id        uuid REFERENCES automation_runs(id),
  CHECK ((status IN ('Approved', 'Declined')) = (decided_by IS NOT NULL))
);
CREATE UNIQUE INDEX ux_run_request_open ON automation_run_requests (activity_id) WHERE status = 'Pending';

DO $$
DECLARE tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY['agent_identities', 'agent_actions', 'automation_run_requests'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tbl);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id()) '
                   'WITH CHECK (tenant_id = current_tenant_id())', tbl);
  END LOOP;
END $$;

-- Before an agent's call is authenticated the API doesn't know the organisation; this answers only that.
CREATE FUNCTION agent_tenant(p_key_hash text) RETURNS uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
  AS $$ SELECT tenant_id FROM agent_identities WHERE key_hash = p_key_hash $$;
REVOKE ALL ON FUNCTION agent_tenant(text) FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    -- Agents are created, rotated and given scopes only by the operations script. The API may suspend one.
    GRANT SELECT ON agent_identities TO app_user;
    GRANT UPDATE (last_used_at, suspended_at, suspended_by, suspended_reason) ON agent_identities TO app_user;
    GRANT SELECT, INSERT ON agent_actions TO app_user;
    GRANT USAGE ON SEQUENCE agent_actions_id_seq TO app_user;
    GRANT SELECT, INSERT, UPDATE ON automation_run_requests TO app_user;
    GRANT EXECUTE ON FUNCTION agent_tenant(text) TO app_user;
  END IF;
END $$;

COMMIT;
