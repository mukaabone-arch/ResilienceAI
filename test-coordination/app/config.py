import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Application role: must NOT own the tables, so RLS is enforced.
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://app_user:app@/resilience?host=/tmp")
    # Worker role with BYPASSRLS, used only to enumerate tenants for background jobs.
    worker_database_url: str = os.getenv(
        "WORKER_DATABASE_URL", "postgresql+psycopg://postgres@/resilience?host=/tmp")
    # "dev" accepts X-Tenant-Id / X-User-Subject headers. "oidc" accepts Microsoft Entra ID access tokens.
    auth_mode: str = os.getenv("AUTH_MODE", "dev")
    # Microsoft Entra ID (AUTH_MODE=oidc). One app registration: a single-page app that also exposes the API.
    # ENTRA_TENANT_ID is the directory people sign in to, or "organizations" to allow several banks' directories.
    entra_tenant_id: str = os.getenv("ENTRA_TENANT_ID", "")
    entra_client_id: str = os.getenv("ENTRA_CLIENT_ID", "")
    # Access tokens must be issued for this API (v2 tokens carry the app's client id as the audience).
    entra_api_audience: str = os.getenv("ENTRA_API_AUDIENCE", "")
    entra_api_scope_name: str = os.getenv("ENTRA_API_SCOPE_NAME", "access_as_user")
    entra_authority_host: str = os.getenv("ENTRA_AUTHORITY_HOST", "https://login.microsoftonline.com")
    entra_jwks_url: str = os.getenv("ENTRA_JWKS_URL", "https://login.microsoftonline.com/common/discovery/v2.0/keys")
    entra_issuer_template: str = os.getenv("ENTRA_ISSUER_TEMPLATE", "https://login.microsoftonline.com/{tid}/v2.0")
    # Roles: when the token carries Entra app roles, they replace the stored roles at each sign-in.
    entra_roles_from_token: bool = os.getenv("ENTRA_ROLES_FROM_TOKEN", "true").lower() == "true"
    # First sign-in: link a person provisioned by email to their Entra account.
    entra_bind_by_email: bool = os.getenv("ENTRA_BIND_BY_EMAIL", "true").lower() == "true"
    # "production" refuses to start with development-only settings (see check_production()).
    app_env: str = os.getenv("APP_ENV", "development")
    ack_sla_hours: int = int(os.getenv("ACK_SLA_HOURS", "48"))
    notification_max_attempts: int = int(os.getenv("NOTIFICATION_MAX_ATTEMPTS", "5"))
    portal_base_url: str = os.getenv("PORTAL_BASE_URL", "https://resilience.example.com")
    # Notifications. "simulated" records deliveries without sending. "m365" sends Outlook mail and calendar invites
    # through Microsoft Graph (app-only) and Teams messages through a Teams Workflows webhook.
    notify_mode: str = os.getenv("NOTIFY_MODE", "simulated")
    graph_tenant_id: str = os.getenv("GRAPH_TENANT_ID", os.getenv("ENTRA_TENANT_ID", ""))
    graph_client_id: str = os.getenv("GRAPH_CLIENT_ID", "")
    graph_client_secret: str = os.getenv("GRAPH_CLIENT_SECRET", "")
    graph_sender: str = os.getenv("GRAPH_SENDER", "")            # mailbox ResilienceAI sends from, e.g. resilience@bank.example
    graph_base_url: str = os.getenv("GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0")
    teams_webhook_url: str = os.getenv("TEAMS_WEBHOOK_URL", "")   # Teams Workflows "when a webhook request is received"
    # Evidence files. "local" keeps them on disk for development; production uses encrypted object storage.
    evidence_store: str = os.getenv("EVIDENCE_STORE", "local")
    evidence_dir: str = os.getenv("EVIDENCE_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "evidence-store"))
    evidence_max_mb: int = int(os.getenv("EVIDENCE_MAX_MB", "25"))
    # Pre-checks on each uploaded file (dates, recovery targets, environment, reuse, sensitive data, relevance).
    # Rule-based checks always run locally; with AI_PROVIDER=anthropic a model also judges relevance and writes the
    # reviewer briefing from a masked, name-free excerpt. "off" disables pre-checks entirely.
    evidence_precheck: str = os.getenv("EVIDENCE_PRECHECK", "on")
    evidence_lookback_days: int = int(os.getenv("EVIDENCE_LOOKBACK_DAYS", "14"))
    # EVIDENCE_STORE=s3: any S3-compatible store (AWS S3, MinIO, others). Credentials come from the standard AWS
    # sources (environment, instance or workload role); set EVIDENCE_S3_ENDPOINT for a non-AWS store.
    evidence_s3_bucket: str = os.getenv("EVIDENCE_S3_BUCKET", "")
    evidence_s3_endpoint: str = os.getenv("EVIDENCE_S3_ENDPOINT", "")
    evidence_s3_region: str = os.getenv("EVIDENCE_S3_REGION", "ap-south-1")
    evidence_s3_prefix: str = os.getenv("EVIDENCE_S3_PREFIX", "evidence/")
    # Server-side encryption for every object: "aws:kms" (with EVIDENCE_S3_KMS_KEY_ID) or "AES256".
    evidence_s3_sse: str = os.getenv("EVIDENCE_S3_SSE", "aws:kms")
    evidence_s3_kms_key_id: str = os.getenv("EVIDENCE_S3_KMS_KEY_ID", "")
    # Object Lock retention: files can't be deleted or overwritten for this many days (0 = no lock).
    # COMPLIANCE: no one can shorten it, administrators included. GOVERNANCE: specially permitted admins can.
    evidence_retention_days: int = int(os.getenv("EVIDENCE_RETENTION_DAYS", "0"))
    evidence_retention_mode: str = os.getenv("EVIDENCE_RETENTION_MODE", "COMPLIANCE")
    # Malware scanning. "builtin" only recognises the EICAR test file (development). "clamav" uses a clamd daemon.
    scanner: str = os.getenv("SCANNER", "builtin")
    clamd_host: str = os.getenv("CLAMD_HOST", "localhost")
    clamd_port: int = int(os.getenv("CLAMD_PORT", "3310"))
    clamd_timeout_s: float = float(os.getenv("CLAMD_TIMEOUT_S", "30"))
    # Assistant. "rules" composes everything from the records with no external service.
    # "anthropic" lets Claude rewrite that text (names hidden, facts only, checked, with fallback).
    ai_provider: str = os.getenv("AI_PROVIDER", "rules")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    ai_model: str = os.getenv("AI_MODEL", "claude-sonnet-5-5")
    ai_timeout_s: float = float(os.getenv("AI_TIMEOUT_S", "20"))


    @property
    def entra_audiences(self) -> list[str]:
        aud = self.entra_api_audience or self.entra_client_id
        return [a for a in {aud, f"api://{aud}"} if a and a != "api://"]

    @property
    def entra_api_scope(self) -> str:
        return f"api://{self.entra_client_id}/{self.entra_api_scope_name}"


settings = Settings()


def check_production(s: Settings = settings) -> list[str]:
    """Settings that must never reach production. The API refuses to start if any apply."""
    if s.app_env != "production":
        return []
    problems = []
    if s.auth_mode == "dev":
        problems.append("AUTH_MODE=dev lets anyone act as anyone; production needs AUTH_MODE=oidc")
    if ":app@" in s.database_url or "postgres:postgres@" in s.database_url:
        problems.append("DATABASE_URL uses the development password")
    if s.auth_mode == "oidc" and not (s.entra_client_id and s.entra_tenant_id):
        problems.append("AUTH_MODE=oidc needs ENTRA_CLIENT_ID and ENTRA_TENANT_ID")
    if s.notify_mode == "m365" and not (s.graph_tenant_id and s.graph_client_id and s.graph_client_secret and s.graph_sender):
        problems.append("NOTIFY_MODE=m365 needs GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET and GRAPH_SENDER")
    if s.evidence_store == "local":
        problems.append("EVIDENCE_STORE=local keeps evidence on the container's disk; use encrypted object storage")
    if s.evidence_store == "s3" and not s.evidence_s3_bucket:
        problems.append("EVIDENCE_STORE=s3 needs EVIDENCE_S3_BUCKET")
    if s.evidence_store == "s3" and s.evidence_s3_sse == "aws:kms" and not s.evidence_s3_kms_key_id:
        problems.append("EVIDENCE_S3_SSE=aws:kms needs EVIDENCE_S3_KMS_KEY_ID (or use AES256)")
    if s.evidence_retention_days <= 0:
        problems.append("EVIDENCE_RETENTION_DAYS is 0, so evidence could be deleted; set the bank's retention period")
    if s.scanner != "clamav":
        problems.append("SCANNER must be clamav in production; the builtin scanner only recognises a test file")
    if s.ai_provider not in ("rules", "anthropic"):
        problems.append(f"AI_PROVIDER={s.ai_provider} isn't recognised; use rules or anthropic")
    if s.ai_provider == "anthropic" and not s.anthropic_api_key:
        problems.append("AI_PROVIDER=anthropic needs ANTHROPIC_API_KEY (without it every AI feature would quietly fall back "
                        "to rules); set the key from the vault, or AI_PROVIDER=rules")
    return problems
