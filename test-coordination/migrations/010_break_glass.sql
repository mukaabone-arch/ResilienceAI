-- ResilienceAI — Test Coordination, migration 010: break-glass access (roadmap R1.3)
-- When Microsoft Entra ID is unavailable, a small number of named holders can still sign in with a passphrase and an
-- authenticator code. A session needs a second holder to approve it, lasts a limited time, and every step is audited.
BEGIN;

CREATE TABLE break_glass_accounts (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  user_id           uuid NOT NULL UNIQUE REFERENCES users(id),   -- a separate person record, idp_subject 'breakglass:…'
  email             varchar(320) NOT NULL,
  passphrase_hash   text NOT NULL,                              -- scrypt, salted
  totp_secret       varchar(64) NOT NULL,                       -- base32, for the holder's authenticator app
  last_totp_step    bigint,                                     -- a code can be used only once
  failed_attempts   integer NOT NULL DEFAULT 0,
  locked_until      timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  rotated_at        timestamptz,
  disabled_at       timestamptz,
  UNIQUE (tenant_id, email)
);

CREATE TABLE break_glass_sessions (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  account_id        uuid NOT NULL REFERENCES break_glass_accounts(id),
  request_code      varchar(12) NOT NULL,                       -- read out to the approver, e.g. 7K3Q-M9PD
  token_hash        char(64) NOT NULL UNIQUE,                   -- sha256 of the bearer token; the token itself is never stored
  reason            text NOT NULL,
  status            varchar(20) NOT NULL DEFAULT 'Pending'
                    CHECK (status IN ('Pending', 'Active', 'Ended', 'Expired', 'Rejected')),
  requested_at      timestamptz NOT NULL DEFAULT now(),
  requested_from    varchar(80),
  approved_by       uuid REFERENCES break_glass_accounts(id),
  approved_at       timestamptz,
  expires_at        timestamptz,
  ended_at          timestamptz,
  ended_reason      text,
  CHECK (approved_by IS NULL OR approved_by <> account_id),    -- nobody approves their own session
  CHECK ((status = 'Active') <= (approved_by IS NOT NULL AND expires_at IS NOT NULL))
);
CREATE INDEX ix_bg_sessions_open ON break_glass_sessions (tenant_id, status) WHERE status IN ('Pending', 'Active');

ALTER TABLE break_glass_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE break_glass_accounts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON break_glass_accounts USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());
ALTER TABLE break_glass_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE break_glass_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON break_glass_sessions USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());

-- Before a request is authenticated the API doesn't know the organisation, and its login can read only its own.
-- These two functions answer exactly the questions it needs and nothing more.
CREATE FUNCTION tenant_for_code(p_code text) RETURNS uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
  AS $$ SELECT id FROM tenants WHERE upper(code) = upper(p_code) $$;
REVOKE ALL ON FUNCTION tenant_for_code(text) FROM PUBLIC;

CREATE FUNCTION break_glass_tenant(p_token_hash text) RETURNS uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
  AS $$ SELECT tenant_id FROM break_glass_sessions WHERE token_hash = p_token_hash $$;
REVOKE ALL ON FUNCTION break_glass_tenant(text) FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    -- Accounts are created and rotated only by the operations script (table owner); the API may read them and
    -- update the sign-in counters. Sessions are never deleted.
    GRANT SELECT ON break_glass_accounts TO app_user;
    GRANT UPDATE (last_totp_step, failed_attempts, locked_until) ON break_glass_accounts TO app_user;
    GRANT SELECT, INSERT ON break_glass_sessions TO app_user;
    GRANT UPDATE (status, approved_by, approved_at, expires_at, ended_at, ended_reason) ON break_glass_sessions TO app_user;
    GRANT EXECUTE ON FUNCTION tenant_for_code(text) TO app_user;
    GRANT EXECUTE ON FUNCTION break_glass_tenant(text) TO app_user;
  END IF;
END $$;

COMMIT;
