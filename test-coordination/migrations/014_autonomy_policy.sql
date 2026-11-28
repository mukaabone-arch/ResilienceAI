-- ResilienceAI — Test Coordination, migration 014: autonomy policy for runbooks and AI agents
-- By default every runbook run needs a person's approval. An autonomy policy, proposed by the model-risk committee
-- and approved by the CISO, can allow named job templates to run without that approval: launched automatically when
-- an activity's prerequisites are done, and/or approved automatically when a named agent asks. Each rule is narrow
-- (templates, test criticality, stages, a daily cap); the policy has a review date and can be revoked at once.
BEGIN;

CREATE TABLE autonomy_policies (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  version         integer NOT NULL,
  status          varchar(20) NOT NULL DEFAULT 'Draft'
                  CHECK (status IN ('Draft', 'Proposed', 'Approved', 'Rejected', 'Superseded', 'Revoked', 'Withdrawn')),
  rules           jsonb NOT NULL DEFAULT '[]'::jsonb,
  rationale       text NOT NULL DEFAULT '',
  review_by       date NOT NULL,                     -- the policy lapses after this date unless a new one is approved
  drafted_by      uuid NOT NULL REFERENCES users(id),
  drafted_at      timestamptz NOT NULL DEFAULT now(),
  proposed_by     uuid REFERENCES users(id),
  proposed_at     timestamptz,
  decided_by      uuid REFERENCES users(id),         -- the CISO
  decided_at      timestamptz,
  decision_note   text,
  revoked_by      uuid REFERENCES users(id),
  revoked_at      timestamptz,
  revoke_reason   text,
  UNIQUE (tenant_id, version),
  CHECK (decided_by IS NULL OR decided_by <> proposed_by),          -- nobody approves their own proposal
  CHECK ((status IN ('Approved', 'Rejected')) <= (decided_by IS NOT NULL AND decision_note IS NOT NULL)),
  CHECK ((status = 'Revoked') = (revoked_by IS NOT NULL))
);
-- At most one approved (in-force) policy per organisation.
CREATE UNIQUE INDEX ux_policy_in_force ON autonomy_policies (tenant_id) WHERE status = 'Approved';

ALTER TABLE autonomy_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE autonomy_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON autonomy_policies USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());

-- A run is approved by a person, or launched under a policy rule (recorded so it can be traced back to the approval).
ALTER TABLE automation_runs ALTER COLUMN approved_by DROP NOT NULL;
ALTER TABLE automation_runs
  ADD COLUMN policy_id   uuid REFERENCES autonomy_policies(id),
  ADD COLUMN policy_rule varchar(120),
  ADD CONSTRAINT automation_runs_who_approved CHECK (approved_by IS NOT NULL OR policy_id IS NOT NULL);

ALTER TABLE automation_run_requests DROP CONSTRAINT automation_run_requests_status_check;
ALTER TABLE automation_run_requests ADD CONSTRAINT automation_run_requests_status_check
  CHECK (status IN ('Pending', 'Approved', 'AutoApproved', 'Declined', 'Expired'));
ALTER TABLE automation_run_requests ADD COLUMN policy_id uuid REFERENCES autonomy_policies(id);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    GRANT SELECT, INSERT, UPDATE ON autonomy_policies TO app_user;
  END IF;
END $$;

COMMIT;
