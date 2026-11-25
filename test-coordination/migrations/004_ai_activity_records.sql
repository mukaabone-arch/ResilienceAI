-- ResilienceAI — Test Coordination, migration 004: AI suggestion log (spec §18.6 ai_activity_records, RA-072)
-- Every AI output is stored with the records it was based on and what a person did with it.
BEGIN;

CREATE TABLE ai_activity_records (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id),
  event_id       uuid REFERENCES test_events(id),
  feature        varchar(40) NOT NULL
                 CHECK (feature IN ('blocker_briefing','slip_forecast','status_summary','message_draft')),
  status         varchar(20) NOT NULL DEFAULT 'Proposed'
                 CHECK (status IN ('Proposed','Accepted','Edited','Dismissed','Superseded')),
  content        jsonb NOT NULL,                    -- what was suggested (headline, text, structured result)
  sources        jsonb NOT NULL DEFAULT '[]'::jsonb, -- the records it was based on: [{type, id, label}]
  provider       varchar(30) NOT NULL,              -- rules | anthropic
  model          varchar(80),
  prompt_version varchar(40) NOT NULL,
  fallback_reason text,                             -- why an external model's text was not used
  slot_key       varchar(80),                       -- scheduled runs: one per event, feature and slot
  requested_by   uuid REFERENCES users(id),         -- null when generated on a schedule
  created_at     timestamptz NOT NULL DEFAULT now(),
  decided_by     uuid REFERENCES users(id),
  decided_at     timestamptz,
  final_content  jsonb,                             -- what the person actually used, when they edited it
  feedback       text,
  CHECK ((status IN ('Accepted','Edited','Dismissed')) = (decided_by IS NOT NULL))
);
CREATE INDEX ix_ai_event ON ai_activity_records (tenant_id, event_id, feature, created_at DESC);
CREATE UNIQUE INDEX ux_ai_slot ON ai_activity_records (tenant_id, event_id, feature, slot_key) WHERE slot_key IS NOT NULL;

ALTER TABLE ai_activity_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_activity_records FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ai_activity_records USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    -- No DELETE: suggestions are kept for oversight; only their decision fields change.
    GRANT SELECT, INSERT, UPDATE ON ai_activity_records TO app_user;
  END IF;
END $$;

COMMIT;
