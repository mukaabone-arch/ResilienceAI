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
    # "dev" accepts X-Tenant-Id / X-User-Subject headers. "oidc" requires a verifier (see security.py).
    auth_mode: str = os.getenv("AUTH_MODE", "dev")
    ack_sla_hours: int = int(os.getenv("ACK_SLA_HOURS", "48"))
    notification_max_attempts: int = int(os.getenv("NOTIFICATION_MAX_ATTEMPTS", "5"))
    portal_base_url: str = os.getenv("PORTAL_BASE_URL", "https://resilience.example.com")
    # Evidence files. "local" keeps them on disk for development; production uses encrypted object storage.
    evidence_store: str = os.getenv("EVIDENCE_STORE", "local")
    evidence_dir: str = os.getenv("EVIDENCE_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "evidence-store"))
    evidence_max_mb: int = int(os.getenv("EVIDENCE_MAX_MB", "25"))
    # Assistant. "rules" composes everything from the records with no external service.
    # "anthropic" lets Claude rewrite that text (names hidden, facts only, checked, with fallback).
    ai_provider: str = os.getenv("AI_PROVIDER", "rules")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    ai_model: str = os.getenv("AI_MODEL", "claude-sonnet-5-5")
    ai_timeout_s: float = float(os.getenv("AI_TIMEOUT_S", "20"))


settings = Settings()
