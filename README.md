# Newtonite — Operations Under Pressure

Internal web application for creating, assigning, tracking, investigating, approving
and resolving operational work across teams.

The system is built as one coherent vertical slice: a team-scoped work-item API with a
server-enforced workflow, an append-only activity history, explicit handling of concurrent
and retried requests, and a React client that shows the server's answer rather than its own
guess at it.

> **Current status.** The vertical slice runs end to end: API, database and web application.
> The README is kept in step with what actually runs. See
> [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) for the honest
> phase-by-phase state, and [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) for
> what is deliberately missing.

## How it fits together

```mermaid
flowchart LR
    subgraph Browser
        UI["React SPA<br/>token held in memory only"]
    end

    subgraph "API process (FastAPI)"
        API["Routers<br/>validate, authorize, delegate"]
        SVC["Services<br/>workflow · policy · paging"]
        AUTH["Session auth<br/>opaque bearer token"]
    end

    subgraph "Worker process"
        W["Outbox worker<br/>FOR UPDATE SKIP LOCKED"]
    end

    DB[("PostgreSQL 16<br/>state · append-only history<br/>outbox · idempotency keys")]

    UI -->|"HTTPS, Bearer token"| API
    API --> AUTH
    API --> SVC
    SVC -->|"one transaction:<br/>state + activity + outbox event"| DB
    W -->|"claim pending events"| DB
    W -->|"insert one notification per event"| DB
    UI -->|"poll unread count"| API
    API -->|"read state, history, notifications"| DB
```

Two boundaries carry most of the design. **Every mutation writes its state change, its
history event and any outbox event in one database transaction**, so a crash cannot leave
history describing something that did not happen. And **the worker is a separate process**
that reads the queue from the same database, so a slow notification never delays an
assignment and a deploy never silently stops delivery. The reasoning is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) 23.

## What works today (through Phase 9 — activity feed and asynchronous delivery)

| Capability | Status |
| --- | --- |
| Backend project, pinned dependencies (`uv`, Python 3.12) | Working |
| Async SQLAlchemy + PostgreSQL wiring, bounded connection pool | Working |
| Alembic migration workflow with a model/migration drift check | Working |
| Schema for users, teams, memberships, work items, comments, activity events, sessions, idempotency keys | Working |
| Database-enforced invariants (team/assignee consistency, append-only history, value sets) | Working, tested |
| Deterministic, idempotent demo seed (3 000 work items, 12 users, 4 teams) | Working |
| Indexes matched to query patterns, with measured plans | Working — see [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) |
| Login, logout and session validation (Argon2id, hashed tokens, absolute + idle expiry, revocation) | Working |
| Per-team authorization with an explicit role matrix, enforced on the server | Working |
| Teams, membership and roster endpoints; admin-only role management | Working |
| One error envelope: `400` / `401` / `403` / `404` / `409` / `412` / `422` / `428` with stable codes | Working |
| Work-item create, read, edit and list, all authorized against team membership | Working |
| Server-side filtering (`status`, `priority`, `type`, `assignee=me\|none`), full-text search, keyset pagination | Working |
| Atomic claim — two concurrent claims produce at most one owner | Working, tested concurrently |
| Stale-update rejection: `If-Match` required (`428`), stale writes refused (`412`) | Working, tested |
| Idempotent creation via a required `Idempotency-Key`, safe under retry and concurrency | Working, tested concurrently |
| Server-enforced workflow with per-transition role permissions; `status` not patchable | Working |
| Activity history written in the same transaction as every mutation | Working, tested by breaking the insert on purpose |
| Comments on an item, idempotent and append-only, readable by any team member | Working |
| Activity read API: one item's timeline and a team's feed, each with its own cursor | Working, read-only by construction |
| Liveness (`/healthz`) and readiness (`/readyz`) endpoints | Working |
| Transactional outbox: events queued in the mutation's transaction, never committed early | Working, tested by rollback |
| Outbox worker: batches with `FOR UPDATE SKIP LOCKED`, retries with backoff, terminal `failed` state | Working, tested with two workers |
| Exactly-once notification *effect* from at-least-once delivery (unique constraint) | Working, tested under replay |
| In-app notifications: list, unread count, mark one/all read, team-scoped and self-scoped | Working |
| Cross-team "My work" (`/me/items`), scoped by the caller's memberships, not a parameter | Working |
| Needs-attention views: assigned to me, unassigned urgent, awaiting approval — one click, server-filtered | Working |
| React + TypeScript web application (Vite, TanStack Query, strict types) | Working |
| Sign-in, team switcher, item list with server-side filters and cursor paging | Working |
| Item detail: claim, workflow moves, edit, comments, history | Working |
| Optimistic claim with tested rollback and reconciliation to the server's state | Working, tested |
| Team activity feed: names and links each row's item, date-range filters in the UI and an additional `actor` filter on the API, paged with its own cursor | Working |
| Loading, empty, error and conflict states on every server-backed view | Working |
| Backend test suite against real PostgreSQL | Working (237 tests) |
| Frontend test suite (vitest + React Testing Library) | Working (83 tests) |
| One-command stack: `docker compose up --build` runs database, API, worker and web | Working, verified end to end |
| Docker Compose PostgreSQL for reviewers | Working |
| CI: backend and frontend jobs, formatting, lint, types, migrate, seed, tests, build | Working |

## Where to look first

The assessment asks for meaningful behaviour beyond CRUD and suggests aiming for four or
five. Seven are implemented and tested. Each row below names the behaviour, the test that
proves it, and the document that explains the reasoning — so a reviewer can check the claim
rather than take it on trust.

| Behaviour | The test that proves it | The reasoning |
| --- | --- | --- |
| **Two concurrent claims produce at most one owner** | `tests/test_items_concurrency.py` — two `POST /claim` calls are issued concurrently on separate connections; exactly one returns `200` | [`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) 5, [`docs/API.md`](docs/API.md) |
| **A write based on a stale version is refused, not applied** | `tests/test_items_update.py`, `tests/test_transitions.py` — a `PATCH` with an old `ETag` returns `412` and the stored row is unchanged | [`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) 6 |
| **A retried request cannot create a duplicate** | `tests/test_items_create.py`, `tests/test_comments.py` — the same `Idempotency-Key` twice creates one row and replays the first result; a different payload under the same key returns `409` | [`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) 7 |
| **Workflow and role rules are enforced on the server** | `tests/test_transitions.py`, `tests/test_authorization.py` — invalid transitions are refused, a viewer cannot write, and another team's item is a `404` | [`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) 8, 11, 12 |
| **History cannot diverge from the state it describes** | `tests/test_activity_atomicity.py` — the activity insert is broken on purpose and the item mutation rolls back with it | [`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) 9 |
| **A notification is delivered once, after the change that caused it** | `tests/test_outbox.py` — a broken outbox insert rolls the assignment back; two workers drain one batch with `SKIP LOCKED`; replaying an event cannot create a second notification; retries stop at the attempt limit | [`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) 23 |
| **An optimistic update is reconciled with the server** | `frontend/src/features/items/ItemDetailPage.test.tsx` — a claim shows immediately, a lost race (`409`) rolls it back and shows the winner, and the next write uses the `ETag` the claim produced | [`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) 24 |

A one-command check of the first four, over real HTTP against the seeded database, is in
[`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) (Phase 4 and Phase 5
sections) — the scripts are not committed, but the commands and the observed results are.

## Technology stack

| Layer | Choice |
| --- | --- |
| API | Python 3.12, FastAPI, Pydantic v2 |
| Persistence | PostgreSQL 16, SQLAlchemy 2.x (async), asyncpg |
| Migrations | Alembic |
| Tests | pytest, pytest-asyncio, HTTPX (ASGI transport), real PostgreSQL |
| Quality | ruff (format + lint), mypy (strict) |
| Frontend | React + TypeScript (Vite, TanStack Query) |
| Worker | the same image and codebase (`python -m app.worker`), no broker |

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) — installs Python 3.12 and the locked dependencies.
- PostgreSQL 16 — either a local server or the bundled Docker Compose service.
- Docker (for the Compose database, or for the one-command stack).
- Node.js `>=20.19` and npm — only for the manual frontend path (`npm ci`, `npm run dev`); the Docker one-command stack does not need them.

## One command: the whole stack

If Docker is available, this is the shortest path to a running application — database,
migrations, demo data, API, outbox worker and the web UI:

```bash
docker compose up --build
```

Open **http://localhost:8080** and sign in as `lena.lead@newtonite.dev` with
`Newtonite!2026` (every demo account is listed in step 5). The first build takes a couple of
minutes; later starts take seconds.

What happens: `db` starts and reports healthy; `api` waits for it, applies migrations,
seeds the demo data (idempotently) and serves on `:8000`; `worker` starts once the API is
ready and delivers notifications; `web` serves the built single-page app on `:8080` and
forwards `/api` to the API. Because the browser talks to one origin, the containerised setup
needs no CORS permission and no host name baked into the image.

| URL | Purpose |
| --- | --- |
| http://localhost:8080 | The web application (through nginx) |
| http://localhost:8080/healthz | Liveness, through the same origin |
| http://localhost:8000/docs | OpenAPI UI, directly |
| http://localhost:5433 | PostgreSQL (host port, for a database client) |

`docker compose down` stops it and keeps the data; `docker compose down -v` also deletes the
database. The rest of this README describes the development setup, which is what to use if
you want to run the tests.

## Setup, step by step

```bash
git clone https://github.com/mahinigam/newtonite-ops-23131011193.git
cd newtonite-ops-23131011193
```

### 1. Configuration

```bash
cp .env.example .env
```

`.env` is gitignored. The values in `.env.example` are **local development placeholders**,
not secrets. Adjust `DATABASE_URL` if your PostgreSQL differs.

### 2. Start PostgreSQL

Either a local server:

```bash
brew services start postgresql@16
```

```sql
CREATE ROLE newtonite LOGIN PASSWORD 'newtonite_dev_pw';
CREATE DATABASE newtonite OWNER newtonite;
CREATE DATABASE newtonite_test OWNER newtonite;
```

Or the bundled Compose service, which listens on **host port 5433** so it never collides
with a PostgreSQL already using 5432:

```bash
docker compose up -d db
# then point .env at it:
# DATABASE_URL=postgresql+asyncpg://newtonite:newtonite_dev_pw@127.0.0.1:5433/newtonite
# TEST_DATABASE_URL=postgresql+asyncpg://newtonite:newtonite_dev_pw@127.0.0.1:5433/newtonite_test
```

`newtonite_test` is required. The integration suite **truncates** its tables between
tests, so it must never point at a database holding data you care about.

### 3. Install dependencies and migrate

```bash
cd backend
uv sync --group dev
uv run alembic upgrade head
```

Equivalently, from the repository root: `make setup` and `make migrate`.

### 4. Load demo data

```bash
cd backend
uv run python -m scripts.seed            # 3000 work items (default)
uv run python -m scripts.seed --items 200  # smaller dataset
```

or `make seed` (add `ITEMS=200` for a smaller set).

The seed is **idempotent**: a second run creates nothing, and importantly generates no
duplicate activity history. Identifiers, teams, roles and the item distribution are
derived from fixed seeds, so the dataset is the same on every machine. Timestamps are
relative to the moment it runs, so the dashboard looks current.

Seeding is additive rather than converging — editing the generator does not rewrite rows
that already exist. To regenerate from scratch, rebuild the schema
(see [`docs/OPERATIONS.md`](docs/OPERATIONS.md)) and run it again.

### 5. Log in as a demo user

All demo accounts use the same development-only password:

| Email | Password | Teams and roles |
| --- | --- | --- |
| `ada.admin@newtonite.dev` | `Newtonite!2026` | platform: admin, support: admin |
| `lena.lead@newtonite.dev` | `Newtonite!2026` | platform: lead |
| `marcus.lead@newtonite.dev` | `Newtonite!2026` | support: lead |
| `priya.lead@newtonite.dev` | `Newtonite!2026` | finance: lead |
| `omar.lead@newtonite.dev` | `Newtonite!2026` | compliance: lead |
| `nina.member@newtonite.dev` | `Newtonite!2026` | platform: member |
| `noah.member@newtonite.dev` | `Newtonite!2026` | platform: member |
| `tara.member@newtonite.dev` | `Newtonite!2026` | support: member |
| `ivan.member@newtonite.dev` | `Newtonite!2026` | finance: member |
| `grace.member@newtonite.dev` | `Newtonite!2026` | compliance: member |
| `sam.multi@newtonite.dev` | `Newtonite!2026` | platform: member, finance: viewer |
| `vic.viewer@newtonite.dev` | `Newtonite!2026` | platform: viewer |

These are **development placeholders, not secrets**, and they exist so a reviewer can
exercise every role without creating accounts. `sam.multi` deliberately holds a different
role in each of two teams — the case a per-team role model has to get right.

Try it without a client:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"sam.multi@newtonite.dev","password":"Newtonite!2026"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

curl -s http://localhost:8000/api/v1/auth/me    -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/v1/teams      -H "Authorization: Bearer $TOKEN"
```

`/auth/me` returns the caller's role in each team, which is how the frontend knows what to
offer. It is advisory to the interface only — every write is authorized again on the server.
The full endpoint list is in [`docs/API.md`](docs/API.md).

### 6. Create, claim and move a work item

The three headers that carry the correctness guarantees, in one sequence. Everything below
runs against a seeded database. Sign in as `sam.multi`, who is a member of the **platform**
team, and replace `<TEAM_ID>` with that team's id from `/auth/me` (its `team_key` is
`platform`). Claiming and editing work there are allowed for a member; the later assignment
uses the platform lead's token.

```bash
# Create — Idempotency-Key is required, so a retry cannot make a second item.
ITEM=$(curl -s -X POST "http://localhost:8000/api/v1/teams/<TEAM_ID>/items" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"title":"Refund the duplicate charge","type":"payment","priority":"urgent"}')
echo "$ITEM"                       # 201 plus the item JSON; the ETag is in the headers
ID=$(echo "$ITEM" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# Claim — atomic. No If-Match: the race is decided by assignee_id IS NULL inside the UPDATE.
curl -s -X POST "http://localhost:8000/api/v1/items/$ID/claim" \
  -H "Authorization: Bearer $TOKEN"

# Edit — If-Match is required. Send the ETag you last saw, or get 428 / 412.
curl -s -X PATCH "http://localhost:8000/api/v1/items/$ID" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'If-Match: "2"' -d '{"priority":"high"}'

# Move it. There is no endpoint that writes status directly.
curl -s -X POST "http://localhost:8000/api/v1/items/$ID/transitions" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'If-Match: "3"' -d '{"to":"in_progress"}'

# List, filtered and paged on the server.
curl -s "http://localhost:8000/api/v1/teams/<TEAM_ID>/items?assignee=none&priority=urgent&limit=5" \
  -H "Authorization: Bearer $TOKEN"
```

### 7. Comment on an item and read its history

```bash
# Comment — Idempotency-Key is required, so a double-click cannot post the note twice.
curl -s -X POST "http://localhost:8000/api/v1/items/$ID/comments" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"body":"Root cause was the 22:00 deploy."}'

# Read the thread, newest first. Each list has its own cursor.
curl -s "http://localhost:8000/api/v1/items/$ID/comments?limit=25" \
  -H "Authorization: Bearer $TOKEN"

# The item's timeline, and the whole team's feed. Both are read-only.
curl -s "http://localhost:8000/api/v1/items/$ID/activity" -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/v1/teams/<TEAM_ID>/activity" -H "Authorization: Bearer $TOKEN"
```

A comment and its `commented` history event are written in one transaction, so a comment
cannot exist without the timeline entry that explains it. There is no route that writes an
event — history is produced by the operations it describes.

### 8. Assign work and read the notification

A notification is delivered **after** the change that caused it, so this also demonstrates
the outbox. The worker must be running — step 9 starts it, or use `make worker`. Replace
`<NINA_USER_ID>` with Nina's `user_id` from `/api/v1/teams/<TEAM_ID>/members`.

```bash
# The platform lead and Nina sign in separately; requests act as their own user.
LEAD_TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"lena.lead@newtonite.dev","password":"Newtonite!2026"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
NINA_TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"nina.member@newtonite.dev","password":"Newtonite!2026"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# A lead assigns an item to somebody else. Assigning another user requires the lead role;
# the assignment and the outbox event are written in one transaction, and the event is not
# delivered by this request. The item version is 4 after create, claim, edit and transition.
curl -s -X PATCH "http://localhost:8000/api/v1/items/$ID" \
  -H "Authorization: Bearer $LEAD_TOKEN" -H 'Content-Type: application/json' \
  -H 'If-Match: "4"' -d '{"assignee_id":"<NINA_USER_ID>"}'

# A moment later, Nina's own token sees it.
curl -s "http://localhost:8000/api/v1/notifications" \
  -H "Authorization: Bearer $NINA_TOKEN"
curl -s "http://localhost:8000/api/v1/notifications/unread-count" \
  -H "Authorization: Bearer $NINA_TOKEN"

# Everything assigned to the caller, across every team — the scope comes from the token.
curl -s "http://localhost:8000/api/v1/me/items?limit=5" \
  -H "Authorization: Bearer $NINA_TOKEN"
```

Only three events notify: **assigned**, **approved** and **sent back**. Claiming an item,
moving it through the ordinary workflow and unassigning do not — the activity feed already
covers them, and a notification for every status change would be noise. Delivery is
at-least-once by design; the unique constraint on `notifications.outbox_event_id` is what
makes the effect exactly-once. See [`docs/API.md`](docs/API.md) and
[`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) 23.

### 9. Run the API, the worker and the web app

Three processes, or `make dev-api`, `make worker` and `make dev-web`. The API and the web
app work without the worker; the worker is what turns queued events into notifications.

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8000
```

```bash
cd backend
uv run python -m app.worker      # poll forever; --once drains what is due and exits
```

```bash
cd frontend
npm ci
npm run dev      # http://localhost:5173
```

Then open **http://localhost:5173** and sign in with any demo account from step 5.

| URL | Purpose |
| --- | --- |
| http://localhost:5173 | The web application |
| http://localhost:8000/healthz | Liveness |
| http://localhost:8000/readyz | Readiness (performs a database round trip) |
| http://localhost:8000/docs | OpenAPI UI |
| http://localhost:8000/api/v1/openapi.json | OpenAPI schema |

The browser talks to `VITE_API_BASE_URL` (default `http://localhost:8000`) directly. The
backend lists the Vite origin in `CORS_ALLOWED_ORIGINS`, and the dev server uses
`strictPort` so a port clash fails loudly instead of silently moving the browser to an
origin the API does not allow. Both are covered in [`docs/FRONTEND.md`](docs/FRONTEND.md).

## Tests and quality checks

```bash
cd backend
uv run pytest -q             # integration tests against real PostgreSQL
uv run ruff format --check . # formatting
uv run ruff check .          # lint
uv run mypy .                # strict static types
```

```bash
cd frontend
npm run test       # component tests (vitest + React Testing Library)
npm run typecheck  # tsc --noEmit, strict
npm run lint       # eslint
npm run build      # production build
```

From the repository root: `make test`, `make lint`, `make typecheck`, `make check` for all of
them together, and `make build-web` for the production bundle.

The backend suite runs against `TEST_DATABASE_URL` and applies migrations to head itself, so
no manual preparation is needed beyond creating the empty `newtonite_test` database. The
frontend suite needs no database: it stubs `fetch`, so it runs in about two seconds.

Current totals, from the commands above: **237 backend tests** in under a minute and **83
frontend tests** in about 2 s. The suites that carry the domain guarantees are worth opening
first:

| Suite | What it proves |
| --- | --- |
| `tests/test_items_concurrency.py` | two concurrent claims, one owner |
| `tests/test_items_update.py`, `tests/test_transitions.py` | stale versions refused; illegal transitions refused |
| `tests/test_items_create.py`, `tests/test_comments.py`, `tests/test_items_concurrency.py` | retries cannot duplicate; payload reuse is rejected |
| `tests/test_authorization.py`, `tests/test_auth.py` | cross-team and role boundaries, including the last-admin rule |
| `tests/test_activity_atomicity.py` | a failed history write rolls the mutation back |
| `tests/test_outbox.py` | queue-in-transaction, two-worker delivery, replay safety, retry exhaustion |
| `tests/test_notifications.py` | self-scoped reads, unread counts, acknowledgement, cross-team refusal |
| `tests/test_index_inventory.py` | the indexes and named constraints the plans depend on actually exist |

The container image is not exercised by those suites, so it was verified separately:
`docker compose up --build` followed by 26 checks over real HTTP through the nginx origin —
login, claim, assignment, worker delivery, unread count, acknowledgement, `/me/items`,
cross-team `404`, stale `412` and the activity feed. All 26 passed. It was then verified again
from a fresh clone of this repository against an empty database, with the API and the worker
started as ordinary processes: 32/32 checks passed, including idempotent replay and a claim
followed by a transition.

## Repository structure

```
backend/
  alembic/            migration environment and revisions
  app/
    api/              FastAPI routers (thin HTTP layer) and dependencies
    models/           SQLAlchemy ORM models and domain enums
    policy/           the role matrix, the authenticated principal and the workflow
    schemas/          request and response models, separate from the ORM
    security/         password hashing and session tokens
    services/         business rules, testable without a request
    config.py         environment-driven settings
    db.py             async engine and session management
    errors.py         error types and the HTTP error envelope
    main.py           application factory
    worker.py         the outbox worker: claim, deliver, retry, give up
  scripts/            the demo seed, maintenance commands and the query-plan tool
  tests/              integration tests against PostgreSQL
  Dockerfile          the API and worker image (one image, two commands)
  docker-entrypoint.sh  migrate, optionally seed, then run the command
frontend/
  src/
    api/              the HTTP client and the response types
    auth/             the in-memory session store, sign-in and sign-out
    components/       the shell and the four view states
    features/         one directory per feature area: items, notifications, activity
    lib/              formatting, error explanations, the router, the role matrix
    query/            the TanStack Query client, its keys and its invalidation helpers
    test/             the fake network, fixtures and render helpers
  Dockerfile          build the SPA, serve it with nginx
  nginx.conf          one origin: static files plus /api forwarded to the API
docker/
  initdb/             creates the test database on first Compose start
docs/                 architecture, frontend, API, decisions, status and review material
```

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/API.md`](docs/API.md) | Endpoints, authentication, error codes and worked examples |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layers, request flow and where each rule lives |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | Entities, integrity rules, indexes and measured query plans |
| [`docs/FRONTEND.md`](docs/FRONTEND.md) | The web application: views, state model, retry policy, tests |
| [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) | Phase-by-phase status with verification evidence |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Configuration, migrations, diagnosis, safe resets |
| [`ENGINEERING_DECISIONS.md`](ENGINEERING_DECISIONS.md) | Decisions with context, options, trade-offs |
| [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) | What is not built yet, and why |

## Security notes

- No secrets are committed. `.env.example` contains placeholders only.
- Passwords are hashed with Argon2id. Session tokens are 256 bits of randomness and are
  stored **only** as a SHA-256 digest, so a database disclosure does not yield usable
  sessions. A fast digest is used here deliberately, because a random token cannot be brute
  forced; passwords get a memory-hard KDF because they can be.
- Sessions have both an absolute lifetime (12 h) and an idle lifetime (2 h), and logout
  revokes immediately. Expiry is evaluated with the database clock, not the application's.
- Authorization is enforced on the server, per team, from stored memberships. A `team_id`,
  `role` or `user_id` in a request body is never trusted — unknown fields are rejected.
- A resource in a team the caller does not belong to returns `404`, not `403`, so responses
  cannot be used to confirm that another team's data exists.
- Notifications are scoped to the recipient, not to a team parameter, and the database's
  composite foreign key keeps a recipient inside the team the notification is about. Reading
  somebody else's notification is a `404`.
- The browser holds the session token **in memory only** — never in `localStorage` or
  `sessionStorage`. The cost is real and accepted: a page reload signs you out. Web storage
  is readable by any script on the page, so keeping the token out of it is the only option
  where closing a tab destroys the credential. The reasoning, including why `HttpOnly`
  cookies were not chosen, is in [`docs/SECURITY.md`](docs/SECURITY.md).
- Not yet built and documented as such: login rate limiting, password reset, and MFA. See
  [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) for the residual risk.

Deployment caveats, including the recommended `Content-Security-Policy`, are documented
in [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Known limitations

The full list, with the reason and the next step for each item, is in
[`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md). The ones most likely to matter in
a review are:

- **No login rate limiting or account lockout.** The login error is deliberately generic, but
  repeated guessing is not throttled.
- **No hosted deployment, TLS termination or CSP.** The repository is a local review
  environment; [`docs/OPERATIONS.md`](docs/OPERATIONS.md) states what a real deployment must
  add.
- **No email or webhook delivery.** In-app notifications are delivered through the outbox;
  only the second transport is absent.
- **No member-management screen.** The admin-only membership API exists and is tested, but the
  web client has no settings view for it.
- **No browser-driven end-to-end test in CI.** The frontend tests stub the network; the wire
  format was verified separately against a running API.

## Next week I would…

In rough priority order, and stated as work rather than wishes:

1. **A second delivery channel for the outbox.** In-app notifications are built and tested;
   email and a team webhook would reuse the same queue and the same exactly-once constraint,
   and would need the one thing the in-app channel does not: a retry policy for a provider
   that is down for hours, and a way to show a permanently failed delivery to an operator.
2. **Browser-driven end-to-end tests in CI.** Playwright against a seeded database, signing in
   as each role and exercising the claim, approval, comment, conflict and refusal paths. The
   frontend tests stub the network, and the wire format was verified by a script run by hand —
   that should be a permanent gate, not a one-off.
3. **Login rate limiting and lockout.** Per-account and per-IP attempt counters with backoff and
   alerting on spikes. This is the largest remaining security gap and the only one that is a
   realistic attack rather than a residual risk.
4. **A member-management screen.** The API is complete and tested — adding, changing and
   removing members, admin only, with the last-admin and assigned-items rules enforced — but the
   interface has no view for it.
5. **Push instead of polling for the unread badge.** The badge refreshes every 20 seconds,
   which is honest but not instant. Server-sent events would suit it better than a WebSocket —
   one direction, no framing to invent — and would need the worker to signal the API process,
   which is the point at which a broker starts to earn its keep.
6. **Reopen-after-close**, with an explicit authorization rule, a reason, and a history event.
   `CLOSED` is terminal today, which is a deliberate simplification rather than a permanent
   answer.
7. **Deployment**: the images exist and run as one stack, but there is no hosted environment,
   TLS termination, or the recommended `Content-Security-Policy` in front of them.
8. **Measure at a larger scale.** The plans are measured at 3 000 items; a bigger seed with
   `EXPLAIN (ANALYZE, BUFFERS)` would show where the current indexes stop being sufficient, and
   would turn "bounded and indexed" into a number.
9. **Kind-tagged or signed cursors**, if a paginated position ever becomes something a user can
   share or edit. Today a cursor is opaque but unsigned, which is correct for a
   server-generated position and would not be for a URL.
