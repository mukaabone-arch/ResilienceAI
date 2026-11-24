# ResilienceAI web app

Next.js (App Router, TypeScript) frontend for the test coordination service in `../test-coordination`.

## Screens

| Screen | Route | Spec |
|---|---|---|
| My work: your activities, acknowledgements, reviews, upcoming tests | `/workspace` | Screen 06 |
| Tests: programme register with stage filters and search | `/tests` | Screens 02/03 (list view) |
| Test workspace: overview, activities and dependency map, readiness and go, people, timeline, findings and closure | `/tests/[id]` | Screens 04, 05, 07, 08, 09, 10 |
| Command centre: exceptions, upcoming tests, acknowledgements, workload | `/command` | Screen 01 |

Every action goes through the API, so the backend's rules apply: role permissions, the readiness gate, segregation of duties and the audit record.

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

Annual calendar views (month/week), the live control room's real-time push (pages refresh after each action today), direct evidence file upload, and OIDC sign-in.
