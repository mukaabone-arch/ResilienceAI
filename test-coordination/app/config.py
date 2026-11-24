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


settings = Settings()
