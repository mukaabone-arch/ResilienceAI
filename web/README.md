# ResilienceAI web app

Next.js (App Router, TypeScript) frontend for the test coordination service in `../test-coordination`.

## Screens

| Screen | Route | Spec |
|---|---|---|
| My work: your activities, acknowledgements, reviews, upcoming tests | `/workspace` | Screen 06 |
| Tests: programme register with stage filters and search | `/tests` | Screen 03 |
| Calendar: financial-year (April to March) or calendar-year, month and week views, with clash markers and blackout periods | `/calendar` | Screen 02 |
| New test: register one test or a recurring series (weekly to yearly) from a template, with every date clash-checked before saving | `/tests/new` | Screens 02/03, RA-015 |
| Test workspace: overview, activities and dependency map, readiness and go, people, timeline, evidence register (upload, download, verify, review), findings and closure | `/tests/[id]` | Screens 04, 05, 07, 08, 09, 10 |
| Command centre: exceptions, upcoming tests, acknowledgements, workload | `/command` | Screen 01 |

`npm run lint` checks React's hook rules. Every action goes through the API, so the backend's rules apply: role permissions, the readiness gate, segregation of duties and the audit record.

## Run locally

```bash
# 1. API (from ../test-coordination)
python -m scripts.demo                 # resets the database and loads a demo portfolio
uvicorn app.main:app --port 8000

# 2. Web
npm install
npm run dev                            # http://localhost:3000
```

The app proxies `/api/v1/*` to `API_URL` (default `http://localhost:8000`), so no CORS setup is needed.

Sign-in is the development "act as" screen. It lists the seeded people and only works with the API's `AUTH_MODE=dev`. Try these people to see the role differences:

- Meera Iyer, test manager
- Arjun Mehta, programme manager: records go decisions and sees the command centre
- Rahul Das, database engineer: has a blocked activity
- Tom Becker, vendor: sees only his own assignment

## Not yet built

The live control room's real-time push (pages refresh after each action today), and OIDC sign-in.
