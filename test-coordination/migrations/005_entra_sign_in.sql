-- ResilienceAI — Test Coordination, migration 005: Microsoft Entra ID sign-in (roadmap R1.0, spec RA-005)
-- An organisation is linked to its Entra directory (tenant id, the token's `tid`). People sign in with their
-- Entra account; the token's object id (`oid`) becomes their idp_subject.
BEGIN;

ALTER TABLE tenants ADD COLUMN entra_tenant_id uuid UNIQUE;

-- People can be provisioned by email before their first sign-in; the first sign-in binds the Entra object id.
ALTER TABLE users ADD COLUMN idp_bound_at timestamptz;
CREATE UNIQUE INDEX users_tenant_email ON users (tenant_id, lower(email)) WHERE email IS NOT NULL;

-- The API's login can read only its own organisation (row-level security), so it can't find which
-- organisation a token belongs to. This function answers exactly that question and nothing more.
CREATE FUNCTION tenant_for_entra(p_entra_tenant_id uuid) RETURNS uuid
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
  AS $$ SELECT id FROM tenants WHERE entra_tenant_id = p_entra_tenant_id $$;
REVOKE ALL ON FUNCTION tenant_for_entra(uuid) FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    GRANT EXECUTE ON FUNCTION tenant_for_entra(uuid) TO app_user;
  END IF;
END $$;

COMMIT;
