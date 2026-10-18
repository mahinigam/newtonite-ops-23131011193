# Operations

Practical guidance for configuring, running, migrating and diagnosing the Newtonite
application. Commands are written for the repository root unless stated otherwise.

## Configuration

All settings come from environment variables, falling back to `.env` in the repository
root (or `backend/.env`). Copy `.env.example` to `.env` and adjust.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `development`, `test` or `production` |
| `DATABASE_URL` | local dev URL | Primary database used by the API, Alembic and scripts |
| `TEST_DATABASE_URL` | local test URL | Disposable database used **only** by the test suite |
| `SESSION_ABSOLUTE_TTL_HOURS` | `12` | Maximum lifetime of a login session |
| `SESSION_IDLE_TTL_HOURS` | `2` | Session is rejected after this much inactivity |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173` | Comma-separated browser origins allowed to call the API |
| `OUTBOX_BATCH_SIZE` | `20` | Events the worker claims at once |
| `OUTBOX_POLL_INTERVAL_SECONDS` | `1.0` | How long the worker waits when the queue is empty |
| `OUTBOX_MAX_ATTEMPTS` | `5` | Attempts before an event becomes terminally `failed` |
| `LOG_LEVEL` | `INFO` | Python logging level |

`.env` is gitignored. Never commit real credentials; `.env.example` holds placeholders
only.

## Databases

Two databases are used locally:

| Database | Used by | Notes |
| --- | --- | --- |
| `newtonite` | API, migrations, seed | Development data |
| `newtonite_test` | `pytest` | **Truncated between tests** — never point it at real data |

Either run a local PostgreSQL 16 (see `README.md`) or use
`docker compose up -d db`, which listens on host port **5433** so it cannot collide with
a PostgreSQL already on 5432. Remember to update `DATABASE_URL` and `TEST_DATABASE_URL`
if you use Compose, and to create `newtonite_test` there (the Compose init script does it
on first start).

## Migrations

```bash
cd backend
uv run alembic upgrade head        # apply everything
uv run alembic downgrade base      # revert every revision (destructive)
uv run alembic current             # show the applied revision
uv run alembic check               # fail if the models drifted from the migrations
```

`alembic check` is the guard against the most common migration mistake — editing a model
and forgetting the revision. It also runs as part of the test suite
(`tests/test_migrations.py`).

Migration revisions are hand-reviewed after `--autogenerate`. The
append-only trigger on `activity_events` is written manually because autogenerate cannot
express it.

## The outbox worker

Notifications are not delivered by the API. A mutation writes an `outbox_events` row in its
own transaction, and `app/worker.py` delivers it:

```bash
cd backend
uv run python -m app.worker          # poll until stopped (Ctrl-C is a clean shutdown)
uv run python -m app.worker --once    # deliver what is due now, then exit
```

The worker is a **separate process** and must be running for notifications to arrive. The
API is fully functional without it; assignment, approvals and the activity feed do not depend
on delivery. In the containerised stack it is the `worker` service.

What it does, and what to check when it misbehaves:

| Observation | Meaning |
| --- | --- |
| `outbox worker started (batch=…, poll=…)` | The process is running and has a database connection |
| Nothing is logged while the queue is empty | Normal: an idle poll is silent |
| `notification delivery failed` at `WARNING` | One attempt failed; the event was rescheduled with backoff |
| `outbox event exhausted its attempts` at `ERROR` | The event is now `failed` and will not be retried |
| The process exits non-zero at start | The database is unreachable, or migrations have not been applied |

Delivery counts per team are readable by a team admin:

```bash
curl -s http://localhost:8000/api/v1/ops/outbox -H "Authorization: Bearer $ADMIN_TOKEN"
```

`failed` is the only terminal state, so a non-zero value is the signal that something needs a
human: the event's `last_error` is in the table, and the usual causes are a departed recipient
(handled, not a failure) or a bug in the handler. Requeue a failed event by returning it to
`pending` with `attempts = 0`; it is a queue row, not history:

```sql
UPDATE outbox_events
SET status = 'pending', attempts = 0, next_attempt_at = now(), last_error = NULL
WHERE id = :id;
```

Two workers may run at once. They claim with `FOR UPDATE SKIP LOCKED`, so neither blocks and
neither delivers the same event twice in the same batch; if both deliver the same event after
a crash, the unique constraint on `notifications.outbox_event_id` makes the second insert a
no-op.

## Diagnosing problems

| Symptom | Likely cause and fix |
| --- | --- |
| `connection refused` on startup or `/readyz` | PostgreSQL is not running, or `DATABASE_URL` is wrong (`brew services start postgresql@16`, or `docker compose up -d db`) |
| `password authentication failed for user "newtonite"` | The role has not been created, or the password in `.env` differs from the one the role was created with |
| `database "newtonite_test" does not exist` | Create it: `CREATE DATABASE newtonite_test OWNER newtonite;` |
| Tests wipe unexpected data | `TEST_DATABASE_URL` points at a database you care about. Stop, fix the URL, and restore from a backup |
| `alembic check` reports differences | A model changed without a migration (or vice versa). Create a revision and review it |
| `activity_events is append-only` | Something tried to `UPDATE`/`DELETE` history. That is the intended behaviour |
| Login works, then every request returns `401 session_expired` | The session hit its absolute or idle lifetime. Lifetimes are `SESSION_ABSOLUTE_TTL_HOURS` / `SESSION_IDLE_TTL_HOURS`; check the host clock, because a large skew from the database makes freshly issued sessions look stale |
| Login returns `401 invalid_credentials` for an account you can see in `psql` | The password is wrong, or the account is deactivated (`is_active = false`). The response is deliberately identical in both cases |
| A user's role change does not take effect | It does, from their next request — roles are read from `memberships` every request and are not stored in the session |
| Port 5432 already in use by Compose | Compose is mapped to 5433 precisely to avoid this; check `.env` |

Logs are written to standard error at the level set by `LOG_LEVEL`. Set `LOG_LEVEL=DEBUG`
(and `echo=True` in `create_database`) to see SQL.

## Safe local reset

> **These commands destroy data.** They are safe only for the local development and test
> databases created by the steps in `README.md`. Never run them against a database that
> holds data you want to keep, and never against a shared or production environment.

Reset the test database schema only (disposable by definition):

```bash
cd backend
uv run alembic downgrade base && uv run alembic upgrade head
```

Rebuild both local schemas from scratch:

```bash
psql -h 127.0.0.1 -U newtonite -d newtonite      -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
psql -h 127.0.0.1 -U newtonite -d newtonite_test -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
cd backend && uv run alembic upgrade head
```

Verify which database you are about to affect before running anything destructive:

```bash
psql "$DATABASE_URL_NO_SCHEME" -c 'SELECT current_database(), current_user;'
```

## Secret rotation and session invalidation

If a credential is ever exposed:

1. Rotate it in the source system. Deleting a file from the latest commit does **not**
   remove it from Git history — rotate first, then clean history if required.
2. Revoke the affected sessions. All sessions, with SQL:
   ```sql
   UPDATE auth_sessions SET revoked_at = now() WHERE revoked_at IS NULL;
   ```
   Or one account, with the maintenance command:

   ```bash
   cd backend
   uv run python -m scripts.maintenance revoke-sessions --email ada.admin@newtonite.dev
   ```

   Revocation takes effect on the next request; sessions are validated against the
   database every time and are never cached in the process, so no restart is needed.

### Session housekeeping

Expired sessions are refused on read, so they cost correctness nothing — only space:

```bash
cd backend
uv run python -m scripts.maintenance purge-expired-sessions
```

This deletes sessions that expired more than a week ago and reports how many remain,
leaving recent rows in place so a support question about a session can still be answered
from the table. It is the only code path that deletes from `auth_sessions`; nothing
expires a session by deleting it.

### Idempotency keys

`idempotency_keys` rows are not history and are not read after the retry window a client
would reasonably use. There is **no automatic pruning**; the table grows with creation
traffic. It is safe to delete old keys — a deleted key means only that a retry long after
the fact would create a second item, which is exactly why the window should exceed the
longest a client would retry:

```sql
-- Keep a week of retry protection; adjust to the client's retry budget.
DELETE FROM idempotency_keys WHERE created_at < now() - interval '7 days';
```

Nothing on the read path consults this table, so pruning cannot break an existing item.

## Measuring query plans

The performance claims in [`DATA_MODEL.md`](DATA_MODEL.md) are reproducible rather than
asserted:

```bash
cd backend
uv run python -m scripts.explain_lists
```

It prints `EXPLAIN (ANALYZE, BUFFERS)` for the thirteen list shapes the API actually issues,
including the cross-team `/me/items` shape, against whatever is in the connected database. It
imports the service layer's statement builders, so the SQL it explains is the SQL the API
runs. Run it against the seeded
database — on an empty table every plan is a sequential scan, because an index cannot win
when there is nothing to walk past.

## Deployment caveats

- The API is designed to run behind a TLS-terminating proxy. Session tokens are bearer
  credentials and must only travel over HTTPS.
- Restrict `CORS_ALLOWED_ORIGINS` to the real frontend origin. It defaults to a single
  localhost origin and does not use wildcards.
- Set a `Content-Security-Policy` such as
  `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' <api-origin>; frame-ancestors 'none'; base-uri 'self'`.
  This is a deployment responsibility and is deliberately not enforced by the Vite dev
  server.
- Serve the API over HTTPS only, and keep `Authorization` headers out of proxy access
  logs.
- The readiness probe is `/readyz`; use `/healthz` for liveness so a database blip does
  not restart otherwise healthy processes.
- Run the worker under a supervisor (the Compose service uses `restart: unless-stopped`). A
  stopped worker is not an outage — the queue is durable — but it is silent until somebody
  checks `GET /api/v1/ops/outbox` or the process restarts.
- Migrations run from the API container's entrypoint in the Compose stack. If you migrate by
  hand instead, apply them **before** starting the worker, so it never reads a table that does
  not exist yet.
