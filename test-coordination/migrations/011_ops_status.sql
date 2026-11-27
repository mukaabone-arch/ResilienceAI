-- ResilienceAI — Test Coordination, migration 011: operational status for monitoring
-- The worker records a heartbeat with delivery, scanning and AI figures on every pass, and the result of verifying
-- each organisation's audit chain. The API reads these for /health/deep without needing access across organisations.
BEGIN;

CREATE TABLE ops_status (
  check_name  varchar(40) NOT NULL,              -- worker_pass | audit_chain
  scope       varchar(80) NOT NULL,              -- 'installation', or the organisation code for audit_chain
  ok          boolean NOT NULL,
  detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
  checked_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (check_name, scope)
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    GRANT SELECT ON ops_status TO app_user;          -- written only by the worker (owner login)
  END IF;
END $$;

COMMIT;
