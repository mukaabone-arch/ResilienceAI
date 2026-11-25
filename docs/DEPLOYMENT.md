# Deploying ResilienceAI

The stack is five containers: PostgreSQL, a one-shot **migrate** job, the **api** (FastAPI), the **worker** (notification delivery, acknowledgement escalations, live summaries) and the **web** app (Next.js). The API and worker share one image.

```
browser ──► web :3000 ──/api/v1/*──► api :8000 ──► PostgreSQL
                                     worker ─────►     ▲
                                     migrate (runs once, then exits) ┘
```

## Run it on one machine

```bash
cp .env.example .env                 # change both passwords
docker compose up -d --build         # first build takes a few minutes
docker compose --profile demo run --rm demo    # optional: demo data (wipes the database)
```

Open http://localhost:3000. API docs are at http://localhost:8000/docs, bound to the local machine only.

| Task | Command |
|---|---|
| See what's running | `docker compose ps` |
| Follow logs | `docker compose logs -f api worker` |
| Migration status | `docker compose run --rm migrate python -m scripts.migrate --status` |
| Upgrade to a new version | `git pull && docker compose up -d --build` (migrate runs first, the API waits for it) |
| Stop, keep data | `docker compose down` |
| Stop and delete all data | `docker compose down -v` |

## Database migrations

`scripts/migrate.py` applies `migrations/NNN_*.sql` in order, each in its own transaction, and records it in `schema_migrations` with a SHA-256 checksum. It is safe to run on every deploy:

- An advisory lock stops two deploys migrating at once.
- A migration that was changed after it was applied is refused. Fix it with a new migration, never by editing an old one.
- A database created before the runner existed (by an older `scripts.seed --reset` or `scripts.demo`) has tables but no history. Run `python -m scripts.migrate --baseline` once to record the existing files as applied.

Migrations run as the table owner (`WORKER_DATABASE_URL`). The API connects as `app_user`, which owns nothing, so row-level security and the append-only audit grants apply to it.

## Configuration

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `APP_ENV` | api | `development` | `production` turns on the startup guard below |
| `AUTH_MODE` | api | `dev` | `dev` = demo sign-in by header. Production: `oidc` |
| `DATABASE_URL` | api | local socket | Restricted `app_user` login |
| `WORKER_DATABASE_URL` | migrate, worker, demo, api* | local socket | Table owner. *The API uses it only for the dev sign-in directory; don't set it on the API in production |
| `POSTGRES_PASSWORD`, `APP_DB_PASSWORD` | compose | none | Required. The DB init script creates `app_user` with the second |
| `EVIDENCE_STORE` | api | `local` | Production: encrypted object storage |
| `EVIDENCE_DIR` | api | `/data/evidence` in the image | Volume `evidence` in compose |
| `EVIDENCE_MAX_MB` | api | `25` | Keep in step with the web proxy limit (30 MB) |
| `AI_PROVIDER` | api, worker | `rules` | `anthropic` lets Claude reword suggestions; facts and fallback stay rule-based |
| `ANTHROPIC_API_KEY`, `AI_MODEL`, `AI_TIMEOUT_S` | api, worker | empty, `claude-sonnet-5-5`, `20` | Only with `AI_PROVIDER=anthropic` |
| `PORTAL_BASE_URL` | worker | | Links in notifications |
| `ACK_SLA_HOURS`, `NOTIFICATION_MAX_ATTEMPTS` | worker | `48`, `5` | |
| `API_URL` | web build | `http://api:8000` | Build argument: Next.js fixes rewrites at build time |

## Production guard

With `APP_ENV=production` the API refuses to start, naming every problem, if:

- `AUTH_MODE=dev` (anyone could act as anyone),
- `DATABASE_URL` still has a development password,
- `EVIDENCE_STORE=local` (evidence on a container disk).

`GET /health` says the process is up; `GET /ready` returns 503 until the database answers. Point the load balancer at `/ready`.

## Before a bank pilot

The compose file is a complete single-host deployment for demos and internal testing. A production pilot also needs:

1. **Single sign-on.** `AUTH_MODE=oidc` with `verify_oidc_token` wired to the bank's Entra ID tenant (app registration, group-to-role mapping).
2. **Secrets.** Database passwords and the Anthropic key from the bank's vault, not a `.env` file.
3. **TLS.** A reverse proxy or ingress in front of `web` (the API is reached only through the web app). Uvicorn runs with `--proxy-headers`.
4. **Managed PostgreSQL** with automated backups, point-in-time recovery and a tested restore. Back up the evidence store with the same retention.
5. **Evidence storage.** An object-store backend for `app/services/storage.py` with server-side encryption and retention lock, plus a real malware scanner in place of the EICAR hook.
6. **Worker scheduling.** One worker replica (or `python -m scripts.worker --once` from a scheduler every minute). The live-summary slot index makes duplicates harmless, but notifications are delivered by whichever worker claims them.
7. **Assistant.** Keep `AI_PROVIDER=rules` until the bank approves sending pseudonymised text to the Claude API; every suggestion is logged in `ai_activity_records` either way.
8. **Monitoring.** Container logs to the bank's SIEM; alerts on `/ready`, failed notification jobs and audit-chain verification.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:

- **API tests:** ruff, migrations applied twice (the second must report nothing pending), then the full pytest suite against PostgreSQL 16.
- **Web:** ESLint, TypeScript check, production build.
- **Docker stack:** builds both images, starts the stack, loads demo data, checks health, readiness, the web app and the API proxy, and confirms the production guard refuses development settings.
