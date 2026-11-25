# ResilienceAI
Agentic AI repositories

## Quick start

```bash
cp .env.example .env                 # change the passwords
docker compose up -d --build         # database, migrations, API, worker and web app
docker compose --profile demo run --rm demo    # optional demo data
```

Then open http://localhost:3000. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for configuration, upgrades and the production checklist. For development without Docker, follow the module READMEs below.

## Modules

| Module | Description |
|---|---|
| [`web/`](web/) | Next.js frontend for test coordination: My work, Tests, the test workspace (readiness gate, dependency map, timeline, closure) and the Command centre. See its [README](web/README.md). |
| [`test-coordination/`](test-coordination/) | Banking Test Programme & Coordination Centre: FastAPI + PostgreSQL backend for planning, mobilising, coordinating and governing resilience tests (MVP increment 1). See its [README](test-coordination/README.md) and [design notes](test-coordination/docs/DESIGN_NOTES.md). |
