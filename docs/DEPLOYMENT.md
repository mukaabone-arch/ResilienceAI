# Deploying ResilienceAI

The stack is five containers: PostgreSQL, a one-shot **migrate** job, the **api** (FastAPI), the **worker** (notification delivery, acknowledgement escalations, live summaries, scanning
evidence held while the malware scanner was down, and the AI view on evidence pre-checks) and the **web** app (Next.js). The API and worker share one image.

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
| `BREAK_GLASS`, `BREAK_GLASS_MINUTES` | api | `off`, `60` | Emergency access when Entra is unavailable (see "Break-glass access"); sessions last 15 to 240 minutes |
| `DATABASE_URL` | api | local socket | Restricted `app_user` login |
| `WORKER_DATABASE_URL` | migrate, worker, demo, api* | local socket | Table owner. *The API uses it only for the dev sign-in directory; don't set it on the API in production |
| `POSTGRES_PASSWORD`, `APP_DB_PASSWORD` | compose | none | Required. The DB init script creates `app_user` with the second |
| `EVIDENCE_STORE` | api, worker | `local` | `s3` in production: any S3-compatible store (see "Evidence storage and malware scanning") |
| `EVIDENCE_S3_BUCKET`, `EVIDENCE_S3_ENDPOINT`, `EVIDENCE_S3_REGION`, `EVIDENCE_S3_PREFIX` | api, worker | none, AWS, `ap-south-1`, `evidence/` | Endpoint only for a non-AWS store (MinIO, others) |
| `EVIDENCE_S3_SSE`, `EVIDENCE_S3_KMS_KEY_ID` | api | `aws:kms`, none | Server-side encryption for every object; `AES256` where KMS isn't available |
| `EVIDENCE_RETENTION_DAYS`, `EVIDENCE_RETENTION_MODE` | api | `0`, `COMPLIANCE` | Object Lock retention for each file; the bucket must have Object Lock enabled |
| `SCANNER`, `CLAMD_HOST`, `CLAMD_PORT` | api, worker | `builtin`, `localhost`, `3310` | `clamav` in production |
| `EVIDENCE_DIR` | api | `/data/evidence` in the image | Volume `evidence` in compose |
| `EVIDENCE_MAX_MB` | api | `25` | Keep in step with the web proxy limit (30 MB) |
| `AI_PROVIDER` | api, worker | `rules` | `anthropic` lets Claude reword assistant suggestions, and judge evidence relevance and write the reviewer briefing. Facts, checks and fallback stay rule-based |
| `ANTHROPIC_API_KEY`, `AI_MODEL`, `AI_TIMEOUT_S` | api, worker | empty, `claude-sonnet-5-5`, `20` | Only with `AI_PROVIDER=anthropic`; the key from the vault |
| `EVIDENCE_PRECHECK` | api, worker | `on` | `off` turns evidence pre-checks off (see "Evidence pre-checks") |
| `EVIDENCE_LOOKBACK_DAYS` | api, worker | `14` | How long before a test's start evidence may be dated without being flagged as old |
| `PORTAL_BASE_URL` | worker | | The address people open ResilienceAI at; every link and Acknowledge button uses it |
| `NOTIFY_MODE` | worker | `simulated` | `m365` sends through Outlook (Graph) and Teams (Workflows webhook) |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` | worker | `ENTRA_TENANT_ID`, none, none | App-only Microsoft Graph credentials (a separate app registration; secret from the vault) |
| `GRAPH_SENDER` | worker | none | Mailbox ResilienceAI sends mail and invitations from, e.g. `resilience@bank.example` |
| `TEAMS_WEBHOOK_URL` | worker | none | Teams Workflows webhook URL; leave empty to send mail only |
| `ACK_SLA_HOURS`, `NOTIFICATION_MAX_ATTEMPTS` | worker | `48`, `5` | |
| `AAP_TOKEN` (name set per integration), `AAP_CA_BUNDLE` | api, worker | none | Ansible Automation Platform token from the vault; the bank's CA bundle if AAP uses an internal CA |
| `MONITORING_TOKEN` | api | none | Turns on `GET /health/deep` for monitoring tools (header `X-Monitoring-Token`); from the vault |
| `AUDIT_VERIFY_HOURS` | worker | `24` | How often the worker re-verifies each organisation's audit trail |
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

### Break-glass access (when Entra is unavailable)

If Microsoft Entra ID is down during a test window, named **break-glass holders** can still get in. Turn it on with
`BREAK_GLASS=on` and create at least two holders per organisation (operations, with the owner database login):

```bash
docker compose run --rm migrate python -m scripts.breakglass add BANK priya.nair@bank.example "Priya Nair"
docker compose run --rm migrate python -m scripts.breakglass add BANK omar.shah@bank.example "Omar Shah" RiskCompliance
docker compose run --rm migrate python -m scripts.breakglass list BANK
```

Each `add` prints a passphrase and an authenticator secret (with an `otpauth://` link for any authenticator app)
**once**. Hand them over in person or seal them in the bank's vault. The default role is programme administrator.

How it works:

1. The sign-in page shows **Emergency access** (`/break-glass`). A holder enters the organisation code, their email,
   passphrase, a current authenticator code and the reason (for example the Entra incident number). They get a
   request code such as `7K3Q-M9PD`.
2. They phone a **second holder**, who approves that code on their own device with their own passphrase and code.
   Nobody can approve their own request; a request expires after 15 minutes.
3. The session lasts `BREAK_GLASS_MINUTES`, with a red banner on every page showing who it is and when it ends.
   **End session** stops it early.

Safeguards: the holder acts as a separate person record ("Priya Nair (break-glass)"), so every action is attributed
to the break-glass identity; every request, approval, refusal, failed attempt and ending is in the audit trail and
logged at WARNING for the SIEM; five wrong attempts lock the account for 30 minutes; an authenticator code works
once; errors never say which detail was wrong; the passphrase is stored only as a salted scrypt hash. Risk,
audit and administrators can see open sessions (`GET /api/v1/break-glass/sessions`).

After any use: review the session in the audit trail, then `python -m scripts.breakglass rotate BANK <email>` for
both holders. `disable` stops a holder at once and ends their sessions.

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

## Evidence storage and malware scanning

Production keeps evidence files in **S3-compatible object storage** (AWS S3, or MinIO or another S3-compatible store
on the bank's premises) and scans every upload with **ClamAV**. File contents never go into the database.

| Protection | How |
|---|---|
| Encryption at rest | Every object is written with server-side encryption: `aws:kms` with the bank's key, or `AES256` |
| Integrity | SHA-256 taken in the browser and on the server, sent with the write (the store rejects a corrupted body) and checked on every download; a changed file is never served |
| Retention | Object Lock retain-until date on every file (`EVIDENCE_RETENTION_DAYS`). In `COMPLIANCE` mode no one, administrators and the cloud account's root included, can delete or overwrite it before then |
| Legal hold | Risk and compliance, audit or the programme administrator can place a hold with a reason; the file is kept until it's released. Shown in the Evidence tab and recorded in the audit trail |
| Malware | Infected uploads are refused and never stored. If ClamAV can't be reached, the file is stored but can't be opened or accepted until the worker scans it; a file found infected then is quarantined (tagged, never served) and the submitter and test manager are told |

Before anything is written, every submission rule (who may submit, the test's stage) is checked, so a refused upload
never leaves an undeletable object behind.

### Set up the bucket (bank's cloud or storage team)

1. Create the bucket **with Object Lock enabled** (it can only be turned on at creation on many stores; versioning
   comes with it). Default encryption: SSE-KMS with a key ResilienceAI's role may use.
2. Block all public access. Give ResilienceAI's role only `s3:PutObject`, `s3:GetObject`, `s3:PutObjectLegalHold`,
   `s3:GetObjectLegalHold`, `s3:PutObjectTagging`, `s3:PutObjectRetention`, `s3:GetBucketObjectLockConfiguration` and
   `s3:ListBucket` on that bucket, plus `kms:GenerateDataKey` and `kms:Decrypt` on the key. No delete permissions.
3. Agree the retention period with the bank's records policy, e.g. 2555 days (7 years).

```bash
# .env
EVIDENCE_STORE=s3
EVIDENCE_S3_BUCKET=bank-resilience-evidence
EVIDENCE_S3_KMS_KEY_ID=arn:aws:kms:ap-south-1:123456789012:key/...
EVIDENCE_RETENTION_DAYS=2555
SCANNER=clamav
CLAMD_HOST=<clamd host>
```

`GET /ready` returns 503 if the bucket can't be reached or Object Lock isn't on, so the load balancer won't send
traffic to a misconfigured API. Files uploaded before the switch stay readable from their original store.

### Try it on one machine

`docker compose --profile secure-storage up -d` adds MinIO (bucket created with Object Lock) and ClamAV, using the
values listed in `.env.example`. ClamAV's first virus-definitions download takes a few minutes; until it's ready,
uploads are held as *waiting for malware scan* and the worker scans them once it is.

## Evidence pre-checks

Every uploaded file that can be read (text and log files, CSV, JSON, XML, email, PDF, Word, Excel, PowerPoint) is
checked against the activity or readiness condition it was submitted for, as soon as it is stored and scanned clean.
The result appears on the file in the Evidence tab, and in the upload dialog when something needs a look.

| Check | How | Needs a model? |
|---|---|---|
| Dates | Latest date in the file against the test window (from `EVIDENCE_LOOKBACK_DAYS` before the start to the upload) | No |
| Recovery point / time | Explicit figures (`apply lag`, `RPO achieved`) or the gap between logged events, against the test's RTO and RPO targets | No |
| Environment | Host names and banners that look like UAT, SIT, staging or test systems | No |
| Reuse | Same SHA-256 as another evidence file, in this test or an earlier one | No |
| Sensitive data | Card numbers (Luhn), Aadhaar numbers (Verhoeff), PANs, passwords, keys, connection strings | No |
| Relevance | Rules: key terms from the activity. Model: a judgement with cited lines | Optional |
| Reviewer briefing | Rules: a summary. Model: what the file shows and what seems to be missing | Optional |

Set the RTO and RPO targets when creating a test, or later on its Overview tab (`rto_target_minutes`,
`rpo_target_minutes` on the API); recorded measurement targets are used when these aren't set.

How it stays safe:

- **It never decides.** Accepting a file that has open warnings needs a note from the reviewer. The pre-check is
  logged in the AI oversight log, closed with the reviewer's decision, and marked as agreed or not, so the bank can
  measure how often it is right.
- **People can correct it.** The submitter or a reviewer can mark a finding *Not right* with a reason; it then no
  longer counts as a warning, and the note is kept.
- **Nothing sensitive is repeated.** Every excerpt is masked before it is stored or shown.
- **The model sees as little as possible.** With `AI_PROVIDER=anthropic`, the worker sends at most ~14,000
  characters of masked lines, with people's names replaced. Its answer is used only if it is valid JSON, every
  citation matches a line that was sent, it names no record that wasn't there, and it doesn't recommend accepting or
  rejecting. Otherwise the rule-based result stands and the reason is logged. Uploads never wait for the model.
- **It can't break an upload.** Any error in a pre-check is logged and skipped.

Images can't be read here (no OCR); they get a note asking the reviewer to check them by eye.
`EVIDENCE_PRECHECK=off` turns the feature off.

## Lessons from earlier tests

Nothing to configure: migration `009_lessons` adds the table that records what planners decide. When a test is
opened, its Overview tab lists lessons from related earlier tests: earlier runs of the same series or template, and
tests that covered the same business services (category alone counts only for closed tests). Lessons are worked
out from what those tests already recorded:

- remediation still open from their findings (overdue first)
- activities that were blocked, for how long and why (from the audit trail), flagged when the same activity is
  blocked again now
- missed RTO / RPO targets, activities that overran their due time, evidence that reviewers sent back
- findings and incidents, with their root cause

Each lesson is matched to the activity it concerns in this test (same code and similar title, or a clearly similar
title), grouped when it happened in more than one earlier test, and links to the records it came from. While the test
is being planned, its manager can add a lesson as a readiness condition (it then counts towards the go decision and
shows *Learned from …*) or set it aside with a reason. External users don't see lessons. No external model is used.

## Decision records and the audit pack

Each go, conditional go or no-go seals a snapshot of everything it rested on (conditions, exceptions, blockers,
acknowledgements, who was present, disagreement). The snapshot's SHA-256 is written into the hash-chained audit
trail, and every time a record is shown it is checked against both; an edited record shows *Doesn't match its seal*.
Disagreement added later is insert-only. Nothing to configure (migration `007_decision_records`).

For an examiner, **Audit pack** on a test's Decisions tab downloads a ZIP with the decision records, the test's audit
trail, its timeline and `verify.py`. The examiner runs it with standard Python 3, no ResilienceAI or network needed:

```bash
unzip BANK-DR-2026-001-audit-pack-20261127.zip -d pack && cd pack
python3 verify.py          # ends "RESULT: all checks passed", or "VERIFICATION FAILED" naming what didn't verify
```

`verify.py` recomputes each record's fingerprint, checks it against its audit row, and re-links the audit rows'
hash chain, so editing a decision *and* the pack's checksum list is still caught. Only the programme administrator, programme
managers, risk and compliance, audit, senior management and the test's own managers can download a pack; each
download is recorded in the audit trail.

## Production guard

With `APP_ENV=production` the API refuses to start, naming every problem, if:

- `AUTH_MODE=dev` (anyone could act as anyone), or `AUTH_MODE=oidc` without `ENTRA_CLIENT_ID` and `ENTRA_TENANT_ID`,
- `DATABASE_URL` still has a development password,
- `EVIDENCE_STORE=local` (evidence on a container disk), `s3` without a bucket, or `aws:kms` without a key,
- `EVIDENCE_RETENTION_DAYS=0` (evidence could be deleted), or `SCANNER` other than `clamav`,
- `NOTIFY_MODE=m365` without the Graph credentials and sender,
- `AI_PROVIDER=anthropic` without `ANTHROPIC_API_KEY` (every AI feature would quietly fall back to rules).

`GET /health` says the process is up; `GET /ready` returns 503 until the database answers. Point the load balancer at `/ready`.

## Before a bank pilot

The compose file is a complete single-host deployment for demos and internal testing. A production pilot also needs:

1. **Single sign-on.** Done: Microsoft Entra ID, with break-glass access for an Entra outage (see above). Still to do
   with the bank: issue the break-glass credentials to at least two holders and agree how they are kept.
2. **Secrets.** Database passwords and the Anthropic key from the bank's vault, not a `.env` file.
3. **TLS.** A reverse proxy or ingress in front of `web` (the API is reached only through the web app). Uvicorn runs with `--proxy-headers`.
4. **Managed PostgreSQL** with automated backups, point-in-time recovery and a tested restore. Back up the evidence store with the same retention.
5. **Evidence storage.** Done: S3-compatible storage with encryption, Object Lock retention and legal hold, and ClamAV scanning (see above). Still to agree with the bank: the retention period and the KMS key.
6. **Worker scheduling.** One worker replica (or `python -m scripts.worker --once` from a scheduler every minute). The live-summary slot index makes duplicates harmless, but notifications are delivered by whichever worker claims them.
7. **AI features.** Keep `AI_PROVIDER=rules` until the bank approves sending text to the Claude API under its AI
   policy. With `anthropic`, two things are sent: assistant drafts with people's names replaced, and masked,
   name-free excerpts of evidence files (at most ~14,000 characters each) with the test's targets and rule-check
   titles. Every AI result is logged in `ai_activity_records` with its prompt version and, for evidence, whether the
   reviewer agreed; the Reports page shows the agreement rate. Tested against `claude-sonnet-5-5`; current models
   reject non-default `temperature`, so none is sent.
8. **Monitoring.** Done in the app: `/health/deep` and the System health panel (see "Monitoring"). Still to set up
   with the bank: container logs to the SIEM, and alerts wired to `/health/deep`.

## Runbook automation (Ansible Automation Platform)

ResilienceAI still never runs commands itself. An activity can be **linked to a job template in the bank's Ansible
Automation Platform (AAP)**; a person approves each run; AAP runs it under its own access controls; and ResilienceAI
follows the job, so nobody types updates:

| AAP job | Activity |
|---|---|
| pending / running | In Progress (the person's approval and the exact variables sent are in the audit trail) |
| successful | The job output is stored as evidence (scanned and pre-checked); the activity is submitted for review, or completed if it has no reviewer |
| failed / error | Blocked, with the failed task, host and message and a link to the job; the owner, the test manager and everyone downstream are told |
| canceled | Blocked ("cancelled in Ansible") |

Pausing, aborting or cancelling the test cancels its running jobs. A job running past its limit alerts the owner and
the test manager (it isn't stopped automatically).

### Set up (bank's automation team)

1. **An AAP user for ResilienceAI** with **Execute** permission on only the job templates ResilienceAI may run (and
   read on their jobs). Create a token for it (AAP 2.5: *Access Management > Users > Tokens*) and put it in the vault.
2. Job templates that take variables from ResilienceAI need **Prompt on launch** for variables. ResilienceAI always
   adds `resilienceai_run`, `resilienceai_event` and `resilienceai_activity` (it uses the first to find a job if a
   launch's answer is lost); without the prompt AAP ignores them and the job still runs.
3. Register AAP in ResilienceAI (the API root is `/api/controller/v2` from AAP 2.5, `/api/v2` before):

   ```bash
   docker compose run --rm migrate python -m scripts.automation add-aap BANK "Bank AAP" https://aap.bank.example/api/controller/v2 --token-env AAP_TOKEN
   ```

   It prints a webhook URL and secret once. Set `AAP_TOKEN` (and `AAP_CA_BUNDLE` for an internal CA) on the API and
   worker.
4. **Notifications in AAP** (so updates arrive within seconds): *Administration > Notifiers > Add*, type **Webhook**,
   the printed URL, method POST, header `{"Authorization": "Bearer <secret>"}`. Attach it to the linked job templates
   (or the organisation) for **Start**, **Success** and **Failure**. ResilienceAI treats a notification only as a
   doorbell: it asks AAP for the job's real state. Without notifications the worker still checks every running job
   each minute.

In the app, a test manager uses **Link runbook** on an activity (choosing from the templates the AAP user can see, with
fixed variables); the owner or test manager presses **Run in Ansible** and confirms. Runs need that approval until an
autonomy policy approved by the CISO allows otherwise. `scripts.automation list`, `rotate-webhook` and `disable` manage
the connection.

**Try it without AAP:** `python -m scripts.fake_aap` serves a stand-in on `http://127.0.0.1:8052/api/v2` (token
`dev-aap-token`) with a few DR job templates; one always fails, for demos. Register it with `add-aap ... --token-env
AAP_TOKEN`, then run the API and worker with `AAP_TOKEN=dev-aap-token`. To have it send notifications too, start it
with `FAKE_AAP_WEBHOOK=<printed URL>` and `FAKE_AAP_WEBHOOK_TOKEN=<printed secret>`.

## Monitoring

| Endpoint | For | Answers |
|---|---|---|
| `GET /health` | Liveness probe | The process is up |
| `GET /ready` | Load balancer | 503 until the database answers and the evidence store is set up |
| `GET /health/deep` | Monitoring and alerting (header `X-Monitoring-Token`) | Overall `ok`, `warn` or `fail` with each check; **503 on `fail`** |

The worker records a heartbeat on every pass and re-verifies each organisation's hash-chained audit trail every
`AUDIT_VERIFY_HOURS` (streamed in batches, so large trails are fine). `/health/deep` reads those, so the API never
reads across organisations:

| Check | `fail` (page someone) | `warn` (look today) |
|---|---|---|
| Database, evidence store | Unreachable, or the bucket isn't locked | |
| Background worker | No pass for 5 minutes | |
| Audit trail | A record doesn't verify (it names the organisation and the first bad record) | Not verified yet, or overdue |
| Notifications | | Any gave up in 24 hours, or any waiting over 15 minutes |
| Malware scanning | | Files waiting over 30 minutes, or scan errors |
| Break-glass | | A session is open or waiting for approval |
| AI features (when on) | | Answers fell back to rules in 24 hours |

Suggested alerts: page on HTTP 503 from `/health/deep`; ticket on `"status": "warn"`. A broken audit trail is also
logged at ERROR by the worker. Administrators, risk and audit see the same report as **System health** on the
Reports page (`GET /api/v1/ops/status`).

```bash
curl -s -H "X-Monitoring-Token: $MONITORING_TOKEN" https://resilience.bank.example/health/deep
```

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:

- **API tests:** ruff, migrations applied twice (the second must report nothing pending), then the full pytest suite against PostgreSQL 16.
- **Web:** ESLint, TypeScript check, production build.
- **Docker stack:** builds both images, starts the stack, loads demo data, checks health, readiness, the web app and the API proxy, and confirms the production guard refuses development settings.
