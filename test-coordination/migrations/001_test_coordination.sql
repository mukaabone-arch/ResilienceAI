-- ResilienceAI — Test Coordination domain, migration 001
-- PostgreSQL 15+. Run as the schema owner (NOT as the application role).
-- The application connects as a non-owner role (app_user) so that row-level
-- security and the audit append-only grants are actually enforced.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Helper: current tenant from the per-transaction setting app.tenant_id
CREATE OR REPLACE FUNCTION current_tenant_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

-- ---------------------------------------------------------------------------
-- Tenancy, identity, organisation
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code          varchar(20)  NOT NULL UNIQUE,     -- used as event-code prefix, e.g. BANK
  name          varchar(200) NOT NULL,
  data_residency jsonb NOT NULL DEFAULT '{}'::jsonb, -- §22 provisioning decisions
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  idp_subject   varchar(200) NOT NULL,             -- identity-provider subject; NOT email
  display_name  varchar(200) NOT NULL,
  email         varchar(320),
  department    varchar(120),
  designation   varchar(120),
  roles         text[] NOT NULL DEFAULT '{}',      -- tenant-level roles (spec §5)
  is_external   boolean NOT NULL DEFAULT false,    -- vendors
  is_active     boolean NOT NULL DEFAULT true,
  notification_channels text[] NOT NULL DEFAULT '{Email,Portal}',
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idp_subject),
  UNIQUE (tenant_id, id)
);

CREATE TABLE teams (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  name          varchar(200) NOT NULL,
  lead_user_id  uuid REFERENCES users(id),
  UNIQUE (tenant_id, name)
);

CREATE TABLE team_members (
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  team_id       uuid NOT NULL REFERENCES teams(id),
  user_id       uuid NOT NULL REFERENCES users(id),
  PRIMARY KEY (team_id, user_id)
);

CREATE TABLE business_services (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  code          varchar(60) NOT NULL,
  name          varchar(200) NOT NULL,
  criticality   varchar(20) NOT NULL DEFAULT 'Medium'
                CHECK (criticality IN ('Critical','High','Medium','Low')),
  UNIQUE (tenant_id, code)
);

CREATE TABLE blackout_periods (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  name          varchar(200) NOT NULL,
  starts_at     timestamptz NOT NULL,
  ends_at       timestamptz NOT NULL,
  CHECK (ends_at > starts_at)
);

-- ---------------------------------------------------------------------------
-- Programme and test configuration (§18.1)
-- ---------------------------------------------------------------------------
CREATE TABLE test_programmes (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  name            varchar(200) NOT NULL,
  programme_code  varchar(50)  NOT NULL,
  financial_year  varchar(20)  NOT NULL,
  start_date      date NOT NULL,
  end_date        date NOT NULL,
  status          varchar(30) NOT NULL DEFAULT 'Draft' CHECK (status IN ('Draft','Active','Closed')),
  owner_user_id   uuid NOT NULL REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, programme_code),
  CHECK (end_date >= start_date)
);

CREATE TABLE test_templates (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  template_key      varchar(80) NOT NULL,           -- stable across versions
  name              varchar(200) NOT NULL,
  category          varchar(50) NOT NULL,
  description       text,
  default_duration  integer NOT NULL CHECK (default_duration > 0),  -- minutes
  version           integer NOT NULL DEFAULT 1,
  is_active         boolean NOT NULL DEFAULT true,
  definition        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- activities, readiness items, milestones, dependencies
  created_by        uuid NOT NULL REFERENCES users(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, template_key, version)
);

CREATE TABLE event_code_sequences (
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  scope         varchar(60) NOT NULL,              -- e.g. DR-2027
  last_value    integer NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, scope)
);

CREATE TABLE test_events (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenants(id),
  programme_id          uuid NOT NULL REFERENCES test_programmes(id),
  template_id           uuid REFERENCES test_templates(id),
  event_code            varchar(60) NOT NULL,
  title                 varchar(250) NOT NULL,
  description           text,
  objectives            text,
  business_impact       text,
  category              varchar(50) NOT NULL
                        CHECK (category IN ('Infrastructure','Application','Database','Cyber','Business')),
  criticality           varchar(20) NOT NULL CHECK (criticality IN ('Critical','High','Medium','Low')),
  lifecycle_status      varchar(30) NOT NULL DEFAULT 'Draft'
                        CHECK (lifecycle_status IN ('Draft','Planned','Mobilising','Readiness','Ready',
                                                    'Live','Paused','Closure','Closed','Cancelled')),
  outcome               varchar(30) CHECK (outcome IN ('Successful','Partial','Failed','Aborted','Cancelled')),
  outcome_summary       text,
  planned_start_at      timestamptz NOT NULL,
  planned_end_at        timestamptz NOT NULL,
  timezone              varchar(60) NOT NULL DEFAULT 'UTC',
  actual_start_at       timestamptz,
  actual_end_at         timestamptz,
  owner_user_id         uuid NOT NULL REFERENCES users(id),
  test_manager_user_id  uuid REFERENCES users(id),
  status_reason         text,                     -- last cancel/pause/abort/return reason
  closure_snapshot      jsonb,                    -- frozen at closure (§15)
  version               integer NOT NULL DEFAULT 1,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, event_code),
  UNIQUE (tenant_id, id),
  CHECK (planned_end_at > planned_start_at),
  CHECK (lifecycle_status <> 'Closed' OR outcome IS NOT NULL)
);

CREATE TABLE test_scope_items (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenants(id),
  event_id              uuid NOT NULL REFERENCES test_events(id),
  business_service_id   uuid REFERENCES business_services(id),
  item_type             varchar(40) NOT NULL DEFAULT 'BusinessService', -- BusinessService/Application/Infrastructure
  name                  varchar(200) NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Stakeholders and assignments (§18.2)
-- ---------------------------------------------------------------------------
CREATE TABLE test_stakeholders (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  event_id            uuid NOT NULL REFERENCES test_events(id),
  user_id             uuid NOT NULL REFERENCES users(id),
  team_id             uuid REFERENCES teams(id),
  event_role          varchar(60) NOT NULL,
  responsibility      text,
  assignment_status   varchar(30) NOT NULL DEFAULT 'Invited'
                      CHECK (assignment_status IN ('Invited','Accepted','Declined','Reassigned')),
  is_primary          boolean NOT NULL DEFAULT true,
  alternate_user_id   uuid REFERENCES users(id),
  is_active           boolean NOT NULL DEFAULT true,
  replaced_by_id      uuid REFERENCES test_stakeholders(id),
  assigned_at         timestamptz NOT NULL DEFAULT now(),
  acknowledged_at     timestamptz,
  ended_at            timestamptz
);
-- Active assignment uniqueness; history retained via inactive rows.
CREATE UNIQUE INDEX ux_stakeholder_active ON test_stakeholders (event_id, user_id) WHERE is_active;

CREATE TABLE test_activities (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  event_id          uuid NOT NULL REFERENCES test_events(id),
  stakeholder_id    uuid REFERENCES test_stakeholders(id),
  activity_code     varchar(60) NOT NULL,
  title             varchar(250) NOT NULL,
  description       text,
  category          varchar(50),
  priority          varchar(20) NOT NULL DEFAULT 'Medium' CHECK (priority IN ('Critical','High','Medium','Low')),
  status            varchar(30) NOT NULL DEFAULT 'Not Started'
                    CHECK (status IN ('Not Started','In Progress','Blocked','Submitted','Completed')),
  blocked_reason    text,
  planned_start_at  timestamptz,
  due_at            timestamptz,
  completed_at      timestamptz,
  reviewer_user_id  uuid REFERENCES users(id),
  review_status     varchar(30) NOT NULL DEFAULT 'Not Required'
                    CHECK (review_status IN ('Not Required','Pending','Accepted','Rejected')),
  is_mandatory      boolean NOT NULL DEFAULT true,
  evidence_required boolean NOT NULL DEFAULT false,
  version           integer NOT NULL DEFAULT 1,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, event_id, activity_code),
  CHECK (review_status = 'Not Required' OR reviewer_user_id IS NOT NULL)
);

-- Reassignment history (spec requires it be retained; no table was specified).
CREATE TABLE activity_assignment_history (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             uuid NOT NULL REFERENCES tenants(id),
  activity_id           uuid NOT NULL REFERENCES test_activities(id),
  from_stakeholder_id   uuid REFERENCES test_stakeholders(id),
  to_stakeholder_id     uuid REFERENCES test_stakeholders(id),
  reason                text,
  changed_by            uuid NOT NULL REFERENCES users(id),
  changed_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test_dependencies (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                 uuid NOT NULL REFERENCES tenants(id),
  event_id                  uuid NOT NULL REFERENCES test_events(id),
  predecessor_activity_id   uuid NOT NULL REFERENCES test_activities(id),
  successor_activity_id     uuid NOT NULL REFERENCES test_activities(id),
  dependency_type           varchar(30) NOT NULL DEFAULT 'finish-to-start' CHECK (dependency_type = 'finish-to-start'),
  is_mandatory              boolean NOT NULL DEFAULT true,
  created_at                timestamptz NOT NULL DEFAULT now(),
  UNIQUE (predecessor_activity_id, successor_activity_id),
  CHECK (predecessor_activity_id <> successor_activity_id)
);
-- Same-event and acyclicity are validated in the service (app/domain/dependencies.py).

-- ---------------------------------------------------------------------------
-- Readiness and execution (§18.3)
-- ---------------------------------------------------------------------------
CREATE TABLE test_readiness_items (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  event_id          uuid NOT NULL REFERENCES test_events(id),
  activity_id       uuid REFERENCES test_activities(id),
  title             varchar(250) NOT NULL,
  description       text,
  owner_user_id     uuid REFERENCES users(id),
  status            varchar(30) NOT NULL DEFAULT 'Pending'
                    CHECK (status IN ('Pending','In Progress','Ready','Blocked','Failed')),
  is_mandatory      boolean NOT NULL DEFAULT true,
  evidence_required boolean NOT NULL DEFAULT false,
  confirmed_at      timestamptz,
  confirmed_by      uuid REFERENCES users(id),
  version           integer NOT NULL DEFAULT 1,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test_milestones (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  event_id          uuid NOT NULL REFERENCES test_events(id),
  title             varchar(200) NOT NULL,
  sequence_number   integer NOT NULL,
  planned_at        timestamptz,
  actual_at         timestamptz,
  status            varchar(30) NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending','Completed','Delayed')),
  owner_user_id     uuid REFERENCES users(id),
  UNIQUE (event_id, sequence_number)
);

CREATE TABLE test_execution_logs (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  event_id            uuid NOT NULL REFERENCES test_events(id),
  activity_id         uuid REFERENCES test_activities(id),
  log_type            varchar(50) NOT NULL
                      CHECK (log_type IN ('Status','Announcement','Decision','Blocker','Milestone','Lifecycle')),
  message             text NOT NULL,
  source              varchar(30) NOT NULL CHECK (source IN ('User','System','Integration','AI')),
  actor_user_id       uuid REFERENCES users(id),
  occurred_at         timestamptz NOT NULL,
  recorded_at         timestamptz NOT NULL DEFAULT now(),
  external_reference  varchar(200)
);

-- ---------------------------------------------------------------------------
-- Communication (§18.4) and transactional outbox
-- ---------------------------------------------------------------------------
CREATE TABLE test_communications (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  event_id            uuid NOT NULL REFERENCES test_events(id),
  activity_id         uuid REFERENCES test_activities(id),
  communication_type  varchar(40) NOT NULL
                      CHECK (communication_type IN ('Invitation','Reminder','Escalation','Announcement','ScheduleChange','ApprovalRequest','Notification')),
  subject             varchar(250) NOT NULL,
  content_reference   varchar(300),
  trigger_type        varchar(50) NOT NULL CHECK (trigger_type IN ('Scheduled','Event-driven','Manual')),
  requires_ack        boolean NOT NULL DEFAULT false,
  dedupe_key          varchar(200) NOT NULL,         -- trigger + business key; makes enqueue idempotent
  created_by          uuid REFERENCES users(id),
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, dedupe_key)
);

CREATE TABLE test_acknowledgements (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  communication_id    uuid NOT NULL REFERENCES test_communications(id),
  recipient_user_id   uuid NOT NULL REFERENCES users(id),
  stakeholder_id      uuid REFERENCES test_stakeholders(id),
  status              varchar(30) NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending','Acknowledged','Declined')),
  due_at              timestamptz,
  responded_at        timestamptz,
  response_comment    text,
  UNIQUE (communication_id, recipient_user_id)
);

-- One row per (communication, recipient, channel). Delivery status lives here,
-- because a communication goes to many recipients over several channels.
CREATE TABLE notification_jobs (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  communication_id    uuid NOT NULL REFERENCES test_communications(id),
  recipient_user_id   uuid NOT NULL REFERENCES users(id),
  channel             varchar(30) NOT NULL CHECK (channel IN ('Email','Teams','Outlook','Portal','ITSM')),
  idempotency_key     varchar(200) NOT NULL,
  payload             jsonb NOT NULL,
  delivery_status     varchar(30) NOT NULL DEFAULT 'Queued'
                      CHECK (delivery_status IN ('Queued','Sent','Delivered','Failed','Dead')),
  attempts            integer NOT NULL DEFAULT 0,
  next_attempt_at     timestamptz NOT NULL DEFAULT now(),
  last_error          text,
  provider_message_id varchar(300),
  sent_at             timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX ix_notification_jobs_due ON notification_jobs (delivery_status, next_attempt_at);

-- ---------------------------------------------------------------------------
-- Evidence, issues, remediation, measurements, approvals (§18.5)
-- ---------------------------------------------------------------------------
CREATE TABLE test_evidence (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenants(id),
  event_id            uuid NOT NULL REFERENCES test_events(id),
  activity_id         uuid REFERENCES test_activities(id),
  readiness_item_id   uuid REFERENCES test_readiness_items(id),
  supersedes_id       uuid REFERENCES test_evidence(id),     -- resubmission chain
  evidence_code       varchar(60) NOT NULL,
  evidence_type       varchar(50) NOT NULL CHECK (evidence_type IN ('Report','Screenshot','Log','Approval','ExternalRecord','Other')),
  storage_reference   varchar(500) NOT NULL,
  filename            varchar(255),
  checksum            varchar(128),
  submitted_by        uuid NOT NULL REFERENCES users(id),
  submitted_at        timestamptz NOT NULL DEFAULT now(),
  review_status       varchar(30) NOT NULL DEFAULT 'Pending' CHECK (review_status IN ('Pending','Accepted','Rejected')),
  review_comment      text,
  reviewed_by         uuid REFERENCES users(id),
  reviewed_at         timestamptz,
  UNIQUE (tenant_id, evidence_code),
  CHECK (reviewed_by IS NULL OR reviewed_by <> submitted_by)   -- no self-review
);

CREATE TABLE test_issues (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  event_id      uuid NOT NULL REFERENCES test_events(id),
  activity_id   uuid REFERENCES test_activities(id),
  issue_code    varchar(60) NOT NULL,
  title         varchar(250) NOT NULL,
  description   text,
  severity      varchar(20) NOT NULL CHECK (severity IN ('Critical','High','Medium','Low')),
  issue_type    varchar(40) NOT NULL CHECK (issue_type IN ('Blocker','Finding','Risk','Incident')),
  status        varchar(30) NOT NULL DEFAULT 'Open' CHECK (status IN ('Open','In Progress','Resolved','Closed')),
  root_cause    text,
  owner_user_id uuid REFERENCES users(id),
  due_at        timestamptz,
  created_by    uuid REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, issue_code)
);

CREATE TABLE test_remediation_actions (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id               uuid NOT NULL REFERENCES tenants(id),
  issue_id                uuid NOT NULL REFERENCES test_issues(id),
  action_code             varchar(60) NOT NULL,
  title                   varchar(250) NOT NULL,
  owner_user_id           uuid NOT NULL REFERENCES users(id),
  due_at                  timestamptz NOT NULL,
  status                  varchar(30) NOT NULL DEFAULT 'Open' CHECK (status IN ('Open','In Progress','Submitted','Accepted')),
  resolution_evidence_id  uuid REFERENCES test_evidence(id),
  accepted_by             uuid REFERENCES users(id),
  accepted_at             timestamptz,
  UNIQUE (tenant_id, action_code)
);

CREATE TABLE test_measurements (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  event_id          uuid NOT NULL REFERENCES test_events(id),
  measurement_type  varchar(50) NOT NULL CHECK (measurement_type IN ('RTO','RPO','Availability','Other')),
  target_value      numeric NOT NULL,
  target_unit       varchar(30) NOT NULL,
  actual_value      numeric NOT NULL,
  unit              varchar(30) NOT NULL,
  target_canonical  numeric,          -- seconds / percentage
  actual_canonical  numeric,
  target_met        boolean,          -- computed comparison
  result_status     varchar(30) NOT NULL DEFAULT 'Not Validated' CHECK (result_status IN ('Met','Not Met','Not Validated')),
  source            varchar(30) NOT NULL DEFAULT 'Manual' CHECK (source IN ('Manual','Integration')),
  evidence_id       uuid REFERENCES test_evidence(id),
  recorded_by       uuid REFERENCES users(id),
  validated_by      uuid REFERENCES users(id),
  validated_at      timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test_approvals (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  event_id          uuid NOT NULL REFERENCES test_events(id),
  approval_type     varchar(50) NOT NULL
                    CHECK (approval_type IN ('Plan','Go/No-go','ReadinessException','Resume','Business','RiskReview','Closure','BlackoutOverride')),
  subject_type      varchar(60),     -- e.g. ReadinessItem for exceptions
  subject_id        uuid,
  approver_user_id  uuid NOT NULL REFERENCES users(id),
  decision          varchar(30) NOT NULL CHECK (decision IN ('Approved','Rejected','Conditional')),
  conditions        text,            -- required when Conditional
  comments          text,
  event_version     integer NOT NULL, -- event revision the decision was made against
  readiness_snapshot jsonb,           -- gate result at decision time (go/no-go)
  decided_at        timestamptz NOT NULL DEFAULT now(),
  superseded_at     timestamptz,      -- set when reschedule / readiness regression invalidates it
  policy_version    varchar(40) NOT NULL DEFAULT 'default-v1',
  CHECK (decision <> 'Conditional' OR conditions IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- Audit (append-only, hash-chained per tenant)
-- ---------------------------------------------------------------------------
CREATE TABLE test_event_audit (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  seq             bigint NOT NULL,
  event_id        uuid,                       -- null for programme/tenant-level actions
  actor_user_id   uuid,
  action          varchar(80) NOT NULL,
  entity_type     varchar(60) NOT NULL,
  entity_id       uuid,
  old_values      jsonb,
  new_values      jsonb,
  occurred_at     timestamptz NOT NULL,
  correlation_id  uuid,
  prev_hash       char(64) NOT NULL,
  row_hash        char(64) NOT NULL,
  UNIQUE (tenant_id, seq)
);

CREATE OR REPLACE FUNCTION forbid_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'test_event_audit is append-only (% blocked)', TG_OP;
END $$;
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE OR DELETE ON test_event_audit
  FOR EACH ROW EXECUTE FUNCTION forbid_audit_mutation();
CREATE TRIGGER trg_audit_no_truncate BEFORE TRUNCATE ON test_event_audit
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_mutation();

-- ---------------------------------------------------------------------------
-- Integration support (§18.6) and API idempotency
-- ---------------------------------------------------------------------------
CREATE TABLE integration_connections (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  provider      varchar(40) NOT NULL,          -- ServiceNow / Jira / M365 ...
  config        jsonb NOT NULL DEFAULT '{}'::jsonb,
  secret_ref    varchar(300),                  -- vault reference only; never the secret
  is_enabled    boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE external_references (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  connection_id   uuid REFERENCES integration_connections(id),
  provider        varchar(40) NOT NULL,
  external_id     varchar(200) NOT NULL,
  entity_type     varchar(60) NOT NULL,
  entity_id       uuid NOT NULL,
  external_status varchar(60),
  last_synced_at  timestamptz,
  sync_error      text,
  UNIQUE (tenant_id, provider, external_id)
);

CREATE TABLE idempotency_keys (
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  key             varchar(200) NOT NULL,
  request_hash    char(64) NOT NULL,
  response_status integer NOT NULL,
  response_body   jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, key)
);

-- ---------------------------------------------------------------------------
-- Indexes (§19)
-- ---------------------------------------------------------------------------
CREATE INDEX ix_events_programme   ON test_events (tenant_id, programme_id);
CREATE INDEX ix_events_schedule    ON test_events (tenant_id, planned_start_at, planned_end_at);
CREATE INDEX ix_events_status      ON test_events (tenant_id, lifecycle_status);
CREATE INDEX ix_activities_event   ON test_activities (event_id, status);
CREATE INDEX ix_activities_due     ON test_activities (tenant_id, due_at) WHERE status <> 'Completed';
CREATE INDEX ix_activities_owner   ON test_activities (stakeholder_id);
CREATE INDEX ix_stakeholders_user  ON test_stakeholders (tenant_id, user_id) WHERE is_active;
CREATE INDEX ix_readiness_event    ON test_readiness_items (event_id);
CREATE INDEX ix_exec_logs_event    ON test_execution_logs (event_id, occurred_at);
CREATE INDEX ix_approvals_event    ON test_approvals (event_id, approval_type);
CREATE INDEX ix_audit_event        ON test_event_audit (tenant_id, event_id, seq);
CREATE INDEX ix_ack_recipient      ON test_acknowledgements (recipient_user_id, status);

-- ---------------------------------------------------------------------------
-- Row-level security: every table with tenant_id is isolated by app.tenant_id
-- ---------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOR t IN SELECT table_name FROM information_schema.columns
           WHERE table_schema = 'public' AND column_name = 'tenant_id'
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_tenant_id())
                    WITH CHECK (tenant_id = current_tenant_id())', t);
  END LOOP;
END $$;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_self ON tenants USING (id = current_tenant_id());

-- ---------------------------------------------------------------------------
-- Application role grants (role must exist; created by ops)
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    GRANT USAGE ON SCHEMA public TO app_user;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
    REVOKE UPDATE, DELETE, TRUNCATE ON test_event_audit FROM app_user;
    REVOKE INSERT, UPDATE, DELETE ON tenants FROM app_user;
    GRANT EXECUTE ON FUNCTION current_tenant_id() TO app_user;
  END IF;
END $$;

COMMIT;
