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
| `AUTH_MODE` | api | `dev` | `dev` = demo sign-in by header. Production: `oidc` (Microsoft Entra ID) |
| `ENTRA_TENANT_ID` | api | none | The bank's Entra directory (tenant) id, or `organizations` to accept several linked directories |
| `ENTRA_CLIENT_ID` | api | none | Application (client) id of the ResilienceAI app registration |
| `ENTRA_ROLES_FROM_TOKEN` | api | `true` | Entra app roles in the token replace the person's stored roles at sign-in |
| `ENTRA_BIND_BY_EMAIL` | api | `true` | Link a person provisioned by email to their Entra account at first sign-in |
| `DATABASE_URL` | api | local socket | Restricted `app_user` login |
| `WORKER_DATABASE_URL` | migrate, worker, demo, api* | local socket | Table owner. *The API uses it only for the dev sign-in directory; don't set it on the API in production |
| `POSTGRES_PASSWORD`, `APP_DB_PASSWORD` | compose | none | Required. The DB init script creates `app_user` with the second |
| `EVIDENCE_STORE` | api | `local` | Production: encrypted object storage |
| `EVIDENCE_DIR` | api | `/data/evidence` in the image | Volume `evidence` in compose |
| `EVIDENCE_MAX_MB` | api | `25` | Keep in step with the web proxy limit (30 MB) |
| `AI_PROVIDER` | api, worker | `rules` | `anthropic` lets Claude reword suggestions; facts and fallback stay rule-based |
| `ANTHROPIC_API_KEY`, `AI_MODEL`, `AI_TIMEOUT_S` | api, worker | empty, `claude-sonnet-5-5`, `20` | Only with `AI_PROVIDER=anthropic` |
| `PORTAL_BASE_URL` | worker | | The address people open ResilienceAI at; every link and Acknowledge button uses it |
| `NOTIFY_MODE` | worker | `simulated` | `m365` sends through Outlook (Graph) and Teams (Workflows webhook) |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` | worker | `ENTRA_TENANT_ID`, none, none | App-only Microsoft Graph credentials (a separate app registration; secret from the vault) |
| `GRAPH_SENDER` | worker | none | Mailbox ResilienceAI sends mail and invitations from, e.g. `resilience@bank.example` |
| `TEAMS_WEBHOOK_URL` | worker | none | Teams Workflows webhook URL; leave empty to send mail only |
| `ACK_SLA_HOURS`, `NOTIFICATION_MAX_ATTEMPTS` | worker | `48`, `5` | |
| `API_URL` | web build | `http://api:8000` | Build argument: Next.js fixes rewrites at build time |

## Microsoft Entra ID sign-in

With `AUTH_MODE=oidc` people sign in with their work Microsoft account (authorization code flow with PKCE, in the
browser). The API accepts only Entra v2 access tokens issued for it: signature against Entra's published keys,
issuer, audience, expiry and the `access_as_user` scope. The token's directory selects the organisation and its
object id the person. Demo sign-in and its directory endpoint are switched off.

### 1. Register the app (Entra admin centre, by the bank's identity team)

1. **App registrations → New registration.** Name: ResilienceAI. Supported account types: *this organisational
   directory only* (a SaaS deployment serving several banks uses *any organisational directory*).
   Redirect URI: platform **Single-page application**, `https://<your ResilienceAI address>/`
   (add `http://localhost:3000/` only for a test registration).
2. **Expose an API.** Set the Application ID URI to `api://<client id>` and add a scope named
   `access_as_user` (who can consent: admins and users).
3. **Manifest.** Set the requested access-token version to 2 (`"requestedAccessTokenVersion": 2` under `api`;
   older manifests call it `"accessTokenAcceptedVersion": 2`). ResilienceAI rejects v1 tokens.
4. **API permissions.** Add *My APIs → ResilienceAI → access_as_user* and grant admin consent.
5. **App roles** (recommended). Create one app role per ResilienceAI role, allowed for users/groups, with these
   exact values: `EnterpriseProgrammeAdministrator`, `DRProgrammeManager`, `TestManager`, `TeamLead`,
   `TechnicalStakeholder`, `BusinessOwner`, `RiskCompliance`, `SeniorManagement`, `ExternalVendor`, `Auditor`.
   Then in **Enterprise applications → ResilienceAI → Users and groups** assign people or groups to roles, and set
   *Assignment required* to Yes so only assigned people can sign in.

### 2. Configure ResilienceAI

```bash
# .env
AUTH_MODE=oidc
ENTRA_TENANT_ID=<directory (tenant) id>
ENTRA_CLIENT_ID=<application (client) id>
```

Link the organisation to its directory and add people by work email (their first sign-in links their account):

```bash
docker compose run --rm migrate python -m scripts.entra link BANK <directory (tenant) id>
docker compose run --rm migrate python -m scripts.entra add-user BANK priya.nair@bank.example "Priya Nair" EnterpriseProgrammeAdministrator
docker compose run --rm migrate python -m scripts.entra status BANK
```

Roles given here are used until the person signs in with Entra app roles, which then take over
(`ENTRA_ROLES_FROM_TOKEN=true`). Someone who signs in but hasn't been added sees a message to ask their
administrator; nothing is created for them automatically.

### How it behaves

- Tokens are kept in the browser tab's session storage and renewed silently; closing the tab signs out of
  ResilienceAI (not of Microsoft). **Sign out** clears ResilienceAI and sends the browser to Microsoft to end the
  Microsoft session too.
- A person is identified by their Entra object id, never by email. The email is used once, to link a provisioned
  person at first sign-in; an already-linked person can't be claimed by another account with the same email.
- The API's database login can't read other organisations, so it finds the organisation through a single
  database function (`tenant_for_entra`) that answers only that question.

## Teams and Outlook notifications

ResilienceAI already writes every notification to an outbox in the same transaction as the change that caused it,
one job per person and channel, delivered by the worker with retries. With `NOTIFY_MODE=m365` the worker sends:

| Channel | What arrives | How |
|---|---|---|
| Email | An Outlook message with an **Acknowledge** button when a response is needed | Graph `sendMail` from `GRAPH_SENDER` |
| Outlook | A calendar invitation for the test window; a reschedule updates the same invitation | Graph event in the sender mailbox's calendar, the person as attendee |
| Teams | An Adaptive Card that @mentions the person, with the same **Acknowledge** button | Teams Workflows webhook |

**Acknowledge** opens a one-tap page (`/acknowledge/<id>`): the person signs in (silently, with Entra), sees the
test, their role and the window, and presses Acknowledge or *I can't do this* (with a reason, which notifies the test
manager). Opening the link changes nothing, because mail security scanners open links too. Only the person the
message was sent to can respond.

### 1. Microsoft Graph app (Entra admin centre)

1. Create a second app registration, e.g. *ResilienceAI Notifications* (keep it separate from the sign-in app).
2. **Certificates & secrets:** add a client secret, store it in the vault, pass it as `GRAPH_CLIENT_SECRET`.
3. **API permissions → Microsoft Graph → Application:** `Mail.Send` and `Calendars.ReadWrite`; grant admin consent.
4. **Limit it to one mailbox.** Application permissions apply to every mailbox until restricted. Create a mail-enabled
   security group containing only the sender mailbox, then in Exchange Online PowerShell:
   `New-ApplicationAccessPolicy -AppId <client id> -PolicyScopeGroupId <group> -AccessRight RestrictAccess -Description "ResilienceAI sender only"`
   (or the newer RBAC for Applications in Exchange). Test with `Test-ApplicationAccessPolicy`.

### 2. Teams Workflows (optional)

In Teams, **Workflows → Create → "When a Teams webhook request is received"**, then add **Post card in a chat or
channel**:
- *Post as* Flow bot, *Post in* **Chat with Flow bot**, *Recipient* = `triggerBody()?['recipient']`. Each person
  gets their own message. Or post in a **channel** (one per programme) if the bank prefers.
- *Adaptive Card* = `triggerBody()?['attachments'][0]['content']`.

Copy the webhook URL into `TEAMS_WEBHOOK_URL`. The request body is `{type, recipient, summary, attachments:[card]}`.
If the URL is empty, Teams jobs are recorded as not configured and mail still goes out.

**Upgrade path:** a registered Teams bot could take the acknowledgement inside Teams (Adaptive Card
`Action.Execute`) without opening a page. That needs a Teams app approved by the bank and an inbound endpoint for
Microsoft's Bot Service, so it's left for when the bank approves both.

### 3. Turn it on

```bash
# .env
NOTIFY_MODE=m365
PORTAL_BASE_URL=https://resilience.bank.example
GRAPH_CLIENT_ID=<notifications app client id>
GRAPH_CLIENT_SECRET=<from the vault>
GRAPH_SENDER=resilience@bank.example
TEAMS_WEBHOOK_URL=<workflow URL>
```

Failed deliveries are retried with back-off up to `NOTIFICATION_MAX_ATTEMPTS`, then marked Dead with the error kept
on the job. People's channel preferences are respected, except for escalations and critical messages.

## Production guard

With `APP_ENV=production` the API refuses to start, naming every problem, if:

- `AUTH_MODE=dev` (anyone could act as anyone), or `AUTH_MODE=oidc` without `ENTRA_CLIENT_ID` and `ENTRA_TENANT_ID`,
- `DATABASE_URL` still has a development password,
- `EVIDENCE_STORE=local` (evidence on a container disk),
- `NOTIFY_MODE=m365` without the Graph credentials and sender.

`GET /health` says the process is up; `GET /ready` returns 503 until the database answers. Point the load balancer at `/ready`.

## Before a bank pilot

The compose file is a complete single-host deployment for demos and internal testing. A production pilot also needs:

1. **Single sign-on.** Done: Microsoft Entra ID, see below. Still to add before go-live: break-glass access for an identity-provider outage (roadmap R1.3).
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
