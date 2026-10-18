# Architecture

A modular monolith. One FastAPI process for requests, one worker process for queued delivery,
one PostgreSQL database, and internal boundaries that are enforced by review rather than by a
network. The two processes are the same codebase started with a different command.

## Layers

```
HTTP request
    │
    ▼
app/api/            routers: parse input, call a service, choose a status code
    │  deps.py      authentication and the "authorize me against this team" dependency
    ▼
app/services/       business rules: login, sessions, teams, memberships, work items,
    │              comments, activity, idempotency, pagination, cursors
    │              (the place to look for "what is allowed", independent of HTTP)
    ▼
app/models/         SQLAlchemy ORM: tables, relationships, enum values
    │
    ▼
PostgreSQL          constraints, unique indexes, triggers, the authoritative clock
                    (also the queue: `outbox_events`, read by `app/worker.py`)
```

Two supporting modules sit across those layers:

| Module | Responsibility |
| --- | --- |
| `app/policy/` | The role matrix (`roles.py`), the authenticated caller (`principal.py`), and the work-item state machine (`workflow.py`). The matrix is the single source for permission checks; the last-admin rule (`services/memberships.py`) and the admin gate on `/ops/outbox` are narrow role-specific rules over the same data. `workflow.py` is the only place a transition is declared legal. |
| `app/errors.py` | The exception types and the single HTTP error envelope. |
| `app/api/preconditions.py` | `ETag` construction and `If-Match` parsing — the HTTP spelling of an optimistic-concurrency check, in one place. |
| `app/services/outbox.py` | Enqueueing an event inside the caller's transaction, and the worker's claim/retry bookkeeping. |
| `app/worker.py` | The process that drains the outbox: claim a batch, deliver each event in its own savepoint, commit. |

The direction of every arrow is one-way. Routers import services; services do not import
routers. Services are callable from a test, a script or a worker without constructing a
request, which is why the seed script can reuse none of them and the tests can call all of
them.

## Two processes, one database

The API never delivers a notification. It writes an `outbox_events` row in the same
transaction as the change that caused it, and a separate process does the delivery:

```
mutation ── one transaction ──► work_items + activity_events + outbox_events
                                              │
                          (committed, or not at all)
                                              ▼
worker loop ──► claim pending rows (FOR UPDATE SKIP LOCKED)
            ──► deliver one event in its own savepoint
            ──► mark delivered, or schedule a retry, or give up
```

Three properties follow, and each is tested:

- **Nothing is owed but lost.** The outbox row commits with the mutation, so there is no
  window in which the change exists and the notification was never queued.
- **The request does not wait.** Delivery is not in the request path, so a slow notification
  cannot slow an assignment, and a failing delivery cannot fail it.
- **A restart does not lose the queue.** The queue is a table, so a deploy, a crash or a
  stopped container leaves the work where it was. `SKIP LOCKED` lets more than one worker run
  without either blocking the other.

Delivery is **at-least-once**. The exactly-once part is the effect: `notifications` has a
unique `outbox_event_id` and the consumer inserts with `ON CONFLICT DO NOTHING`, so replaying
an event cannot produce a second notification. The reasoning and the alternatives are in
[`ENGINEERING_DECISIONS.md`](../ENGINEERING_DECISIONS.md) 23.

## Request flow for a protected write

`PUT /api/v1/teams/{team_id}/members/{user_id}` is a representative example.

1. **Transport and shape.** FastAPI validates the path UUIDs and the body against
   `SetRoleRequest`. An unknown field is a `422`, because request models set
   `extra="forbid"` — that is the guard against mass assignment.
2. **Authentication** (`Depends(get_principal)`). The `Authorization` header is parsed, the
   token is hashed, and `auth_sessions` is probed with all validity conditions in one
   `WHERE`: not revoked, not past `expires_at`, `last_used_at` inside the idle window, user
   active. All comparisons use PostgreSQL's `now()`. The caller's memberships are loaded and
   returned as a `Principal`.
3. **Authorization** (`require_team_permission(Permission.MANAGE_MEMBERS)`). The role for
   `{team_id}` is looked up in the principal's memberships. Not a member → `404`. Member but
   insufficient role → `403`.
4. **Business rules** (`services/memberships.set_role`). Is the change legal at all — would
   it remove the last admin, is the target somebody the caller may act on?
5. **Persistence.** A single statement, then commit. The response is built from what the
   database now holds, not from what was requested.

Every step can refuse the request on its own. Nothing decides permissions from the URL alone,
and nothing decides them from the body.

## Request flow for a work-item mutation

`PATCH /api/v1/items/{item_id}` is the harder case, because the team is not in the URL — the
item id is, and an id is what a client actually holds.

1. **Transport and shape.** FastAPI validates the body against `UpdateWorkItemRequest`, whose
   fields are all optional *and* tri-state: a field that is absent means "no opinion", while an
   explicit `null` means "clear it". `model_fields_set` is what distinguishes them, so an
   empty patch and a clearing patch are different requests.
2. **Authentication**, as above. FastAPI resolves the authenticated principal before the handler
   body runs.
3. **Precondition.** The `If-Match` header is parsed to a version integer. Absent → `428`.
   Unparseable → `422`. This happens before the item row is read, so a request that cannot be
   applied never touches the item.
4. **Authorization from the row, not the request.** The service loads the item and derives the
   team **from the item's own `team_id`**, then checks membership against that. An item in
   another team is indistinguishable from a missing one: `404`, with no existence leak. The
   path never carries a team id for a write, so there is nothing to lie about.
5. **Business rules.** The work-item service enforces role-dependent field rules: moving
   work to or from *somebody else* needs `assign others`, while taking an unassigned item is a
   claim. `app/policy/workflow.py` owns only status transitions. `status` is not a field of
   this request at all.
6. **Persistence, once.** The version comparison and `version = version + 1` are in the same
   `UPDATE`. Field-change events are inserted in the same transaction through
   `services/activity.py`, which shares the session. Either the state change and its history
   both land, or neither does.
7. **The response is the row.** Read back after the update and serialized, with a fresh `ETag`.
   Nothing is echoed from the request.

`POST /items/{item_id}/claim` is the same path with one deliberate difference: it takes no
precondition, because its guard is the `UPDATE`'s own `assignee_id IS NULL` predicate rather
than a version. A version check would be weaker — it says nothing about who owns the item.

## Request flow for a comment

`POST /api/v1/items/{item_id}/comments` combines the two mechanisms above and adds a third,
which is why it is worth following separately.

1. **Transport and shape.** The body is validated against `CreateCommentRequest`: stripped,
   then length-checked between 1 and 4 000. There is no author or item field, so neither can be
   claimed by a client.
2. **Authentication**, then **authorization from the row**: the item is loaded and its `team_id`
   is the team the caller is checked against. A non-member is a `404` that is byte-identical to
   the one for an invented id.
3. **Role.** Writing needs `COMMENT`; a viewer reading the same thread is allowed, because
   reading is `VIEW_TEAM`.
4. **Idempotency, in the same transaction.** The key row, the comment and the `commented` event
   are one transaction. A concurrent duplicate blocks on `unique (actor_id, operation, key)`,
   finds the stored outcome and replays it; the same key with a different body is a `409`
   rather than a replay of the wrong comment. Because the key row is written inside the
   transaction, a comment that rolls back does not burn its key.
5. **The response is the row**, read back after commit.

Nothing in this flow bumps the item's `version`: a comment changes no field of the item, so it
is not a new version of it. Commenting on a closed item is allowed — closure ends the workflow,
not the discussion.

## Where each rule lives

| Rule | Enforced by | Why there |
| --- | --- | --- |
| Request shape, field allow-list | Pydantic schemas | Cheapest place; also produces the field-level error a client needs |
| Passwords, session tokens | `app/security/` | Pure functions, no database, easy to test in isolation |
| Session validity and expiry | SQL predicate in `services/auth.py` | One query, database clock, no read-then-check race |
| Team boundary | `Principal.require_team` | Membership is loaded once per request; a non-member is a 404 |
| Role permissions | `app/policy/roles.py` matrix | One reviewable table instead of comparisons scattered through routers |
| Duplicate memberships, invalid enum values, assignee-in-team | PostgreSQL constraints | Must hold even against a code path nobody has written yet |
| Append-only history | PostgreSQL trigger | Same reason: history is not allowed to have an "edit" path |
| Team keeps an admin | `services/memberships.py` | A policy about people, not about rows |
| Which transitions exist, and who may make them | `app/policy/workflow.py` table | The offered transitions and the refusal come from one source, so they cannot disagree |
| A stale update is refused | `If-Match` compared **inside** the `UPDATE` in `services/work_items.py` | A separate read-then-check is exactly the race being prevented |
| One winner per claim | SQL predicate `assignee_id IS NULL AND status = 'open'` inside the `UPDATE` | The database decides the race while it holds the row lock; no window exists |
| One item per retried create | Unique `(actor_id, operation, key)` on `idempotency_keys`, written in the item's transaction | A concurrent duplicate blocks on the index and then replays the stored outcome |
| History cannot diverge from state | One session, one transaction, `services/activity.py` | Proven by breaking the insert on purpose (`tests/test_activity_atomicity.py`) |
| Who may read an item's comments or timeline | `require_item_access` in `app/policy/principal.py`, called by `services/work_items.py` and `services/activity.py`; comments go through `work_items.load_visible_item` | The team comes from the item's own row, so the same rule cannot be forgotten by one of the readers |
| Which list a cursor belongs to | `decode_cursor(..., id_kind=...)` at each route | The id kind is the hard boundary: activity uses a bigint, while item, comment and notification lists use UUIDs. UUID-keyed lists share a syntax, so the client must keep each cursor with its list; mixing them names a different position, not a security boundary |
| Comments are append-only | No route exists; the `activity_events` trigger covers history | A missing route cannot be bypassed, and the history behind a comment is protected by the database |
| A notification is owed the moment the change commits | `outbox_events` written by `services/outbox.py` in the caller's transaction | The queue row and the state change share a commit, so they cannot disagree |
| One event produces at most one notification | `UNIQUE(notifications.outbox_event_id)` + `ON CONFLICT DO NOTHING` | At-least-once delivery is safe because the effect is made idempotent by the database, not by the handler |
| A notification only reaches a member of the team | Composite FK `notifications(team_id, recipient_id) → memberships(team_id, user_id)` | The same boundary as an assignee, enforced in the same way |
| A person reads only their own notifications | `recipient_id = principal.user.id` in `services/notifications.py`; somebody else's id is a `404` | The scope is the token, so it cannot be widened by a parameter |

## The frontend

The client is a separate application with its own entry point, and it is documented in
[`FRONTEND.md`](FRONTEND.md). The architectural facts that belong here are the ones that
constrain the backend:

- **It is not a second source of truth.** Every list, item, comment and event on screen came
  from a response. Filtering, searching, ordering and paging are query parameters, and the
  cursor is handed back verbatim. There is no code path that filters a loaded page.
- **It holds no decision the server makes.** The moves an item offers are the server's
  `available_transitions`, computed for the caller's role. The mirrored role matrix in
  `frontend/src/lib/permissions.ts` decides which *controls to draw*, never what is allowed.
- **It accepts the server's answer rather than inventing one.** A write's response is the new
  state and is adopted directly, including the `ETag`; a refused write is explained and the
  item is re-read. The single exception is the claim, which is shown immediately and rolled
  back from a snapshot on failure — a decision with its own tests and its own section in
  [`ENGINEERING_DECISIONS.md`](../ENGINEERING_DECISIONS.md) 24.
- **The `ETag` is stored with the item, not beside it.** They are one fact — the version of
  that item — and keeping them apart is how a client ends up sending an old version with new
  data.

## Conventions

- **Routers are thin.** A handler validates, calls one service function, and returns a
  schema. If a handler needs a branch that is not about HTTP, the branch belongs in a
  service.
- **Schemas are not models.** Responses are declared explicitly, so a new column is never
  published by accident and a password hash cannot leak into a response.
- **Services raise `AppError` subclasses.** Those carry a status code and a stable `code`,
  but they are plain exceptions — no FastAPI types in the business layer.
- **Errors are registered once.** `register_exception_handlers` in `app/main.py` gives every
  failure, including framework-generated ones, the same JSON shape.
- **Nothing is trusted from the client except the fields it is allowed to set.** Team ids,
  roles, versions, `created_by` and audit fields are always derived on the server.

## Adding an endpoint

1. Add or reuse a schema in `app/schemas/`.
2. Put the rule in `app/services/`, not in the router.
3. Add a route in `app/api/v1/`, declaring `SessionDep` and either `PrincipalDep` or a
   `require_team_permission(...)` dependency.
4. Add a test that proves the refusal as well as the success.
5. Run `make check`.
