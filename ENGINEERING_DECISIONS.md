# Engineering Decisions

Decisions are recorded as they are implemented, in the format the brief asks for:
context, options considered, the choice, why, the trade-offs, and how the decision is
verified. Anything not yet implemented is listed separately at the end so this document
never describes hypothetical behaviour as if it existed.

---

## 1. Modular monolith rather than services

**Context.** The brief describes one product with a small, well-understood domain
(users, teams, work items, comments, activity). The assessment values a smaller system
with well-considered behaviour over a large one with incomplete features, and the system
must be modifiable live during review.

**Options considered.**
1. Modular monolith — one FastAPI process with explicit internal boundaries.
2. Separately deployed services for work items, activity and notifications.
3. A serverless function per endpoint.

**Choice.** A modular monolith with layered boundaries: `api/` (HTTP) → `services/`
(business rules) → `models/` (persistence), with `policy/` holding authorization rules.

**Why.** All the interesting invariants in this domain — "one claimant", "history matches
state", "assignee belongs to the team" — are *cross-cutting* and are easiest to keep
correct when they live in one process and one database transaction. Splitting them across
services would turn a single atomic write into a distributed transaction, weakening
exactly the guarantees the brief asks for. There is also no independent scaling need: the
load is one kind of request against one database.

**Trade-offs.**
- Rejected because of the trade-off: independent deployability and per-service scaling
  would be lost. Neither is needed at this scale, and both would cost transactional
  integrity.
- Accepted cost: module boundaries are conventions rather than network boundaries, so they
  must be enforced by review and by keeping routers thin.

**Consequences and verification.** Routers only translate HTTP ↔ service calls. The
layering is visible in `backend/app/`; `docs/ARCHITECTURE.md` explains the flow.

---

## 2. Relational current state plus append-only activity history, not event sourcing

**Context.** Users must be able to see "what happened previously" and "who is responsible
now". Both a current-state view and a history are required, and history must never
disagree with the current state.

**Options considered.**
1. Current-state tables plus an append-only `activity_events` log, written in the same
   transaction as the change.
2. Full event sourcing — events are the source of truth, current state is a projection.
3. Current state only, with history derived from an audit log written asynchronously.

**Choice.** Option 1.

**Why.** Event sourcing is a heavy commitment: every read needs a projection, schema
evolution needs event upcasting, and the reviewer would have to reason about eventual
consistency everywhere. Writing the event in the same transaction as the state change
gives the property that actually matters — *the history is never inconsistent with the
item* — without that cost. Option 3 was rejected outright because a failure between the
two writes would silently produce a state change with no explanation.

**Trade-offs.**
- Rejected: event sourcing's ability to rebuild arbitrary past states and to add new
  read models retroactively.
- Accepted: history is descriptive rather than authoritative, so it cannot be replayed to
  reconstruct state. Queries always read the current-state tables.

**Consequences and verification.** The append-only property is enforced twice: there is no
application code path that updates or deletes an event, and a database trigger refuses
`UPDATE`/`DELETE` on `activity_events` (`alembic/versions/0001_initial_schema.py`).
Verified by `tests/test_schema_constraints.py::test_activity_history_is_append_only`.

The transactional coupling itself is verified by `tests/test_activity_atomicity.py`, which
installs a trigger that makes *every* insert into `activity_events` fail and then attempts an
edit, a claim and a transition. In each case the response is a generic `500`, and the item is
byte-identical to what it was before — no state change survived its missing history entry.
The test also checks that the failure body leaks no SQL, table name, trigger name or
traceback, and that removing the trigger restores normal behaviour, which shows the failures
came from the trigger rather than from something else.

---

## 3. Data integrity enforced by PostgreSQL constraints, not only by application code

**Context.** Two rules are easy to state and easy to violate accidentally: "an assignee
must be a member of the item's team", and "audit history is immutable". Both were
explicitly called out in the project guidance.

**Options considered.**
1. Application-level validation only.
2. Database constraints only.
3. Both, with the database as the backstop.

**Choice.** Option 3, and specifically:

- a composite foreign key `work_items(team_id, assignee_id) → memberships(team_id, user_id)`,
  resting on a unique constraint on those membership columns;
- a trigger rejecting `UPDATE`/`DELETE` on `activity_events`;
- `CHECK` constraints for status, type, priority, role, version, title and body lengths.

**Why.** Application validation is the right place for good error messages, but it is the
wrong place to rely on for invariants that must never break. A composite foreign key
expresses "assignee must belong to the team" as a single declarative rule that no code
path — including a future one written by someone else — can bypass. PostgreSQL uses
MATCH SIMPLE semantics, so a null assignee skips the check, which is precisely the
behaviour wanted for unassigned items.

**Trade-offs.**
- Rejected: flexibility to assign cross-team items. That is a product non-goal here.
- Accepted: the composite key requires managing two membership uniqueness/index concepts,
  which is documented on the model to avoid confusion later.
- Accepted: enum-like values are stored as text with `CHECK` constraints rather than as
  PostgreSQL `ENUM` types, so adding a status is an additive migration rather than a type
  rewrite. The trade-off is that the value set lives in two places (Python enum and SQL
  check), which the drift check and constraint tests partly cover.

**Consequences and verification.**
`tests/test_schema_constraints.py` proves each rule with raw SQL that bypasses the
application entirely: cross-team assignee rejected, same-team accepted, unassigned
accepted, history immutable.

---

## 4. Schema as reviewed code, with a drift check

**Context.** The project must be reproducible for a reviewer, and migrations are the only
acceptable way to change the schema.

**Options considered.**
1. `Base.metadata.create_all()` at startup.
2. Alembic with `--autogenerate`, committed and reviewed.
3. Hand-written SQL migration scripts.

**Choice.** Option 2, with revisions read and corrected by hand after generation.

**Why.** Option 1 cannot evolve a database that already has data, and hides changes from
review. Hand-written SQL is more control than this project needs and drifts from the
models. Autogenerate is fast and accurate for tables, columns, indexes and foreign keys;
manual review is still required for anything it cannot express — which is exactly what
happened with the append-only trigger in revision `0001`.

**Trade-offs.**
- Accepted: `alembic check` does not compare `CHECK` constraint bodies, so a check
  constraint changed in a model but not in a migration would not be caught by the drift
  test. Mitigated by the explicit constraint tests in `tests/test_schema_constraints.py`,
  and stated here rather than glossed over.

**Consequences and verification.** `tests/test_migrations.py::test_models_and_migrations_do_not_drift`
runs `alembic check` as part of the suite, so drift fails CI. Round-tripping
(`downgrade base` → `upgrade head`) was verified on both local databases.

---

## 5. Asynchronous stack with a bounded connection pool

**Context.** The brief describes hundreds to a few thousand simultaneous users. FastAPI
supports both synchronous endpoints (run in a worker thread pool) and asynchronous ones.

**Options considered.**
1. Async SQLAlchemy 2.x with asyncpg, async endpoints.
2. Synchronous SQLAlchemy with psycopg3, endpoints run in FastAPI's thread pool.

**Choice.** Option 1.

**Why.** A synchronous endpoint occupies a thread for the whole request, so concurrency is
capped by the thread pool regardless of how little work the request does. With async
endpoints, the limiting factor becomes the database pool, which is sized deliberately
(`pool_size=10`, `max_overflow=20`) instead of implicitly by a thread count. It also makes
the concurrency tests natural: two concurrent requests are two coroutines with two
independent sessions, which is exactly how the claim and idempotency races will be
exercised.

**Trade-offs.**
- Rejected: synchronous code is easier to debug (ordinary tracebacks, no accidental
  blocking). Chosen anyway because the concurrency behaviour is the point of the exercise,
  and correctness here comes from SQL conditions rather than from the driver.
- Accepted: every database call must be awaited; a blocking call inside a coroutine would
  stall the event loop. Strict typing plus a narrow repository/service layer keep this
  contained.
- Accepted: a single process is still one failure domain. Horizontal scaling needs a
  shared pool or an external pooler, which is a stated evolution path rather than a
  current claim.

**Consequences and verification.** The pool configuration is explicit in
`backend/app/db.py`. Tests use a `NullPool` engine so independent concurrent sessions
cannot be handed a connection bound to a different event loop.

---

## 6. Database-backed opaque sessions rather than JWTs

**Context.** The API needs to identify a caller on every request, must be able to end a
session immediately, and must not put a credential in the hands of an attacker who reads
the database.

**Options considered.**

1. Signed stateless JWT in a cookie or `Authorization` header.
2. Opaque random token with the server storing a hash of it.
3. Server-side session with a session id in a cookie.

**Choice.** Option 2: a 256-bit token from `secrets.token_urlsafe(32)`, stored only as its
SHA-256 digest, in an `auth_sessions` row with `expires_at`, `last_used_at` and
`revoked_at`.

**Why.** The brief asks for expiration *and* revocation. A JWT is valid until it expires —
revoking one requires either a denylist (which reintroduces the database lookup the JWT was
meant to avoid) or a key rotation that logs everybody out. Since every protected request
already touches the database, a stateless token buys nothing and costs the ability to sign
someone out immediately. Storing only the digest means a database disclosure exposes
expired hashes rather than live credentials. A hand-rolled cookie session adds CSRF
concerns and a second credential concept for no benefit.

**Trade-offs.**

- Accepted: every authenticated request costs one indexed lookup on `token_hash`. Measured
  as a single index probe, which is the same order as the work the request does anyway.
- Accepted: no cross-service portability. There is one service.
- Rejected: the ability to verify a token without the database.

**Sub-decision: the digest is fast (SHA-256), the password hash is slow (Argon2id).** They
have opposite threat models. A 256-bit random token cannot be brute-forced, so a memory-hard
KDF would only add latency to every request. A human password can be guessed offline, so it
gets Argon2id. Both are in `app/security/`.

**Sub-decision: two clocks, one authority.** Absolute and idle expiry are both evaluated with
PostgreSQL's `now()`, never the application's. With more than one process, an application-side
comparison would grant a session a different lifetime depending on which process answered.
`login` also derives `expires_at` from the database clock, so issuance and validation agree.

**Sub-decision: `last_used_at` is refreshed at most once a minute.** The update is conditional
in SQL (`... WHERE last_used_at < now() - interval '60 seconds'`), so reads do not become
writes and two concurrent requests cannot both decide to update the row. The cost is that the
idle window can be enforced up to a minute late; that is recorded in
`docs/KNOWN_LIMITATIONS.md` rather than left implicit.

**Consequences and verification.** `tests/test_auth.py` covers: unknown email and wrong
password returning byte-identical responses; only a digest stored (and the raw token appearing
in no column); missing, malformed and foreign-scheme headers; unknown and revoked tokens;
absolute expiry; idle expiry; activity refreshing the idle window; two logins producing
independent sessions; and an expired token not being resurrected by signing in again.

---

## 7. One error envelope, and 404 rather than 403 across a team boundary

**Context.** Clients need to distinguish "sign in again", "you may not do that", "that is
gone", "somebody changed this" and "your input is wrong" — without learning anything about
resources they cannot see.

**Options considered.**

1. FastAPI's defaults (`{"detail": ...}`, plus a plain-text 500).
2. A single envelope for every failure, produced by exception handlers.
3. Per-route error shapes.

**Choice.** Option 2, with a fixed mapping:

| Status | Code | Meaning |
| --- | --- | --- |
| 401 | `not_authenticated`, `invalid_session`, `session_expired`, `invalid_credentials` | No usable session |
| 403 | `forbidden` | Inside the team, role does not permit it |
| 404 | `not_found` | Does not exist **or** is in a team you are not in |
| 409 | `invalid_transition`, `item_already_claimed`, `idempotency_key_reuse`, `last_admin`, … | Conflicts with current state |
| 412 | `stale_version` | Your `If-Match` version is out of date |
| 422 | `validation_failed` | Request body rejected |

**Why.** `code` is what the frontend branches on; `message` is written to be shown to a user
without leaking anything. The 403/404 split carries the important policy: a caller **inside**
a team whose role is insufficient gets 403, because they already know the team exists, and the
message helps them understand. A caller outside the team gets 404, indistinguishable from a
nonexistent resource, so the API cannot be used to confirm that another team's work item
exists. `tests/test_authorization.py` asserts the two responses are byte-identical for a
foreign team and a random UUID.

**Trade-offs.**

- Accepted: a stricter envelope than FastAPI's default, so framework-level errors (unknown
  route, wrong method) need an adapter handler to avoid a second shape.
- Accepted: the 404 choice is mildly less helpful for a legitimate user who typed a stale
  team URL — the frontend compensates by only ever offering teams from `/auth/me`.
- 500 responses are always the same generic body; the detail goes to the log, never to the
  client.

**Consequences and verification.** `app/errors.py` defines the types and installs the
handlers; `tests/test_authorization.py` and `tests/test_auth.py` assert the status **and** the
`code` for every case, and that a refused write leaves the stored role unchanged.

---

## 8. Authorization as an explicit permission matrix

**Context.** Four roles, several protected actions, and a rule that the server decides — never
the UI, and never a value from the request.

**Options considered.**

1. Role comparisons inline in route handlers (`if role == "admin"`).
2. A declarative matrix: role → set of permissions, plus a principal that carries the caller's
   roles per team.
3. A policy DSL or external authorization service.

**Choice.** Option 2. `app/policy/roles.py` holds the only role logic; `Principal` carries the
roles, loaded from the database on every request; routes declare the permission they need as a
dependency.

**Why.** The matrix is a single reviewable table, so an auditor can answer "can a member close
the item?" by reading eight lines instead of searching for string comparisons. Making it a
dependency means a route cannot accidentally *perform* an action before checking it, and a
route that forgot its check is conspicuous. Option 3 is infrastructure with no payoff at four
roles.

**Trade-offs.**

- Accepted: for `PUT /teams/{id}/members/{user_id}`, that an admin may only set a role for
  somebody who already shares a team with them. It cannot enumerate strangers, and it means
  adding a brand-new person to a team is not possible through the API (seeded users only) —
  recorded in `docs/KNOWN_LIMITATIONS.md`.
- Accepted: admins currently have no capability a lead lacks except membership management.
  The distinction is real but narrow, and `admin` is the role reserved for it.

**Consequences and verification.** `tests/test_authorization.py`: a viewer cannot escalate
anyone's role, a member cannot promote themselves, a lead cannot manage members, an admin can,
and an admin of one team is answered with 404 for another team's roster. The permission table is
also what the frontend will read via `/auth/me` — advisory only, since every write is checked
again.

---

## 9. Claiming is one conditional `UPDATE`, not a read-then-write

**Context.** Two people can claim the same unassigned item at the same moment. The brief
asks for at most one successful claimant. The obvious implementation is also the wrong one:
read the item, check it is unassigned, then write. Between the check and the write, another
request can do the same thing, and both succeed.

**Options considered.**

1. Read the row, check `assignee_id IS NULL` in application code, then `UPDATE`.
2. `SELECT … FOR UPDATE` to lock the row, then check and update.
3. A single `UPDATE … WHERE id = :id AND assignee_id IS NULL AND status = 'open'` whose
   affected-row count *is* the answer.
4. Compare-and-set on a version number, as `PATCH` uses.

**Choice.** Option 3, plus a follow-up read to explain a loss.

**Why.** Option 1 is the bug. Option 2 works but serializes every claim attempt on a row
lock and still needs the same predicate written twice. Option 3 makes the eligibility
condition and the assignment the same statement, so PostgreSQL decides the race inside the
row lock it already holds for the update — there is no window and no separate policy to keep
in sync with the write. Zero rows affected means "you lost", and the follow-up read is used
only to choose a *specific* reason (`item_already_claimed` versus `invalid_transition`)
rather than to make the decision.

Option 4 was rejected for a subtle reason: a version precondition is the *weaker* guard
here. Two claims can carry the same valid version and both pass it; the version says nothing
about who the assignee is. Using `If-Match` for claiming would look rigorous while being
less correct, so the endpoint deliberately asks for no `If-Match` at all.

**Trade-offs.**

- Accepted: a lost race needs a second query to produce a helpful error. That query runs
  only on the losing path, and it cannot change the outcome.
- Accepted: the eligibility rule lives in the SQL predicate as well as in the workflow
  table. They are asserted against each other by tests (`invalid_transition` for a
  non-open item is produced from the same rule set).
- Rejected: pessimistic locking, which would make concurrent claims queue rather than
  letting the database reject the loser immediately.

**Consequences and verification.** `tests/test_items_concurrency.py` fires two, then ten,
genuinely concurrent HTTP requests at one item — real sessions on their own connections, so
the race is real rather than simulated. Exactly one `200` and the rest `409`, one assignee
in the database, and exactly one `claimed` event.

The test's ability to detect the bug was checked by *breaking the implementation on
purpose*: `claim_item` was temporarily rewritten as the read-then-write version with the race
window widened. Both concurrency tests then failed (ten of ten claims succeeded). The
original file was restored and verified byte-identical.

---

## 10. Idempotent creation with a required `Idempotency-Key`

**Context.** A client that times out cannot tell whether its create succeeded. Retrying is
the right thing to do, and must not produce a second item. Double-clicking is the same
problem in a smaller package.

**Options considered.**

1. Nothing — the client accepts possible duplicates.
2. Deduplicate on a content hash of the request.
3. A client-supplied `Idempotency-Key`, unique per caller and operation.
4. A server-assigned request id returned to the client after the fact.

**Choice.** Option 3. The key is **required**, and scoped to `(actor_id, operation, key)`.

**Why.** Option 2 cannot distinguish "the same request sent twice" from "two genuinely
identical items", which is a real thing in this domain (two identical refund requests for
the same customer are plausible). Option 4 cannot help the retry that never received the
first response, which is the case that matters.

Scoping by actor and operation means two people who happen to choose the same key do not
collide, and the same key used for a different operation is not a conflict. A fingerprint of
the canonical JSON body (`sort_keys`, compact separators) is stored alongside the key, so
reusing a key with a *different* payload is `409 idempotency_key_reuse` rather than
silently returning an unrelated item.

**How the race is closed.** The key row and the work item are inserted in **one
transaction**, with `UNIQUE (actor_id, operation, key)`. A concurrent duplicate does not take
a lock and wait politely — it blocks on the unique index until the first transaction
commits or rolls back, then sees either a committed key (and replays) or nothing (and
proceeds). The insert is attempted inside a savepoint (`session.begin_nested()`) so the
`IntegrityError` does not poison the surrounding transaction.

**Why the key is required rather than optional.** If it were optional, every client that
forgot it would silently be a client without retry safety, and the API could not tell a
retry from a genuine second item. Refusing with `400 idempotency_key_required` makes the
choice explicit and impossible to overlook by accident.

**Trade-offs.**

- Accepted: one extra row per create, and a table that needs periodic pruning (documented in
  `docs/OPERATIONS.md`; the rows are a retry window, not history).
- Accepted: the stored response body is a minimal provenance record rather than a full
  cached response — a replay returns a **fresh read** of the item, so it can never return a
  stale representation.
- Accepted: a client that reuses one key for two different items gets a `409`. That is the
  intended contract, and the message says how to fix it ("use a new key").
- Rejected: a time-window-based deduplication, which needs a heuristic and still has an
  arbitrary boundary.

**Consequences and verification.** `tests/test_items_create.py` (17 tests) and
`tests/test_items_concurrency.py`: two concurrent identical creates produce one `201` and one
`200` with the **same id** and exactly one row; a mismatched payload is `409`; a missing key
is `400`; a key that was rejected *before* anything was written can be reused successfully,
so a failed attempt does not burn the key. As with claiming, the test was checked against a
deliberately broken implementation (duplicate keys no longer replayed) and failed as
expected.

---

## 11. Preconditions are required (428) and stale writes are refused (412)

**Context.** Two people edit the same item. Without a guard, the second save silently
overwrites the first person's change, and the first person's edit disappears with no
explanation. The brief asks explicitly for stale updates to be rejected rather than silently
applied.

**Options considered.**

1. Last write wins, with no guard.
2. Compare the incoming `updated_at` with the stored one.
3. Require `If-Match` with the item's version; refuse a mismatch with `412`.
4. Make the precondition optional, and only check it when the client happens to send one.

**Choice.** Option 3, on every update and every transition. A missing header is
`428 precondition_required`; a mismatch is `412 stale_version`.

**Why.** Option 1 is the failure the brief names. Option 2 compares a *timestamp*, which is
both coarser and ambiguous when two writes land in the same millisecond; a monotonic integer
version is unambiguous. Option 4 was rejected because a guard that can be omitted is a guard
that will be omitted by exactly the client that most needs it — and an API cannot then claim
to prevent lost updates. Requiring `If-Match` keeps the claim honest: an edit that did not
demonstrate it had seen the current version is not applied at all.

The comparison and `version = version + 1` happen in the **same `UPDATE`**, so the check
cannot be separated from the write by another transaction.

**Trade-offs.**

- Accepted: it is more work for a client — it must read first, keep the `ETag`, and handle a
  `412` by refreshing. That is the point, and `details.current` carries the current
  representation so the recovery step needs no extra round trip.
- Accepted: `428` is a less familiar status than `400`. It is the correct one (RFC 6585) and
  the message explains what to send.
- Accepted: a request that changes nothing returns `200` without bumping the version or
  writing history, so an idempotent-feeling save from a form does not fill the timeline.

**Consequences and verification.** `tests/test_items_update.py` (15) and
`tests/test_transitions.py` (16) cover: `428` with no header; `412` with `details.current`
for a stale one; a successful edit bumping the version and the `ETag`; per-field events; a
no-op writing nothing; malformed `If-Match` values (`banana`, `"nope"`, `0`, `-3`) rejected as
`422`; and two concurrent editors both sending `If-Match: "1"`, where exactly one `200` and
one `412` result and only one history entry is written.

---

## 12. The workflow is a server-side state machine, and status is not a patchable field

**Context.** Items move through `open → in_progress → awaiting_approval → resolved → closed`,
with approval and closure reserved to leads. A generic `PATCH` that accepted `status` would
be a way around those rules: anyone who can edit an item could set it to `closed`.

**Options considered.**

1. Put `status` in the update payload and validate the value.
2. A dedicated transition endpoint that applies the state machine.
3. Derive status from other fields (for example, "closed when `closed_at` is set").
4. A workflow engine or state-machine library.

**Choice.** Option 2: `POST /items/{id}/transitions` with `{"to": …}`, and `status` is
**not** an accepted field on `PATCH`. Each transition declares its origin, destination,
required permission, and whether an assignee is required.

**Why.** Option 1 makes it impossible to say "who may do this" per edge, because the
permission depends on the *transition*, not on the field: a member may move work forward but
may not approve it. Option 3 spreads the state machine across every writer. Option 4 is
infrastructure for a state machine with fewer than ten edges.

Because the rules are one table (`app/policy/workflow.py`), `available_transitions(status,
role, has_assignee)` is computed from the same source that validates the request, so the
statuses offered in an item response and the transitions the server accepts cannot disagree.
Each edge also records a specific event type — `approved`, `rejected`, `status_changed` —
rather than an anonymous "status changed", so a timeline can be read without decoding
`from_value` and `to_value`.

**Rejection is a first-class outcome.** `awaiting_approval → in_progress` is legal for a lead
and is recorded as `rejected` with the reviewer's note. Sending work back is not a delete, and
the assignee keeps the item — a rejection returns the work, it does not orphan it.

**Trade-offs.**

- Accepted: two endpoints (`PATCH` and `transitions`) instead of one, because the rules
  genuinely differ. `PATCH` refuses `status`, so the split cannot be bypassed.
- Accepted: `closed` is terminal. Reopening needs decisions about who may do it, whether the
  version resets, and what history it writes; guessing would be worse than deferring, so the
  API refuses and a closed item's `available_transitions` is empty. Recorded in
  `docs/KNOWN_LIMITATIONS.md`.
- Accepted: any member may advance any item in their team, not only their own. That is what
  lets a small team cover for one another, and changing *ownership* remains restricted. The
  behaviour is asserted in a test so it is a decision rather than an accident.

**Consequences and verification.** `tests/test_transitions.py`: the whole journey
`open → closed` with each step attributed to the right actor in the history; a member refused
at approval and at closure (`403`); `assignee_required` when starting unowned work; an illegal
jump naming the reachable statuses; `closed` terminal for all four targets; stale `412`;
missing `428`; cross-team `404`; an unknown target `422`; and two leads approving
concurrently, where one wins and one gets `412` with a single `approved` event.

**Closure ends the workflow, not the discussion** (added in Phase 5). A comment on a closed
item is accepted, because a closed item is a record and the reason it closed is exactly the
kind of thing somebody needs to add afterwards. Refusing it would push that note somewhere the
item's history cannot see it. A comment changes no field of the item, so it does not bump
`version` and cannot be mistaken for a reopening — `tests/test_comments.py` asserts both.
Editing or deleting a comment remains excluded: a comment is evidence of what somebody said.

---

## 13. Keyset pagination, with a total order

**Context.** Team item lists must be filtered, searched and paged on the server, and the
brief asks that the browser never be sent the whole dataset.

**Options considered.**

1. `OFFSET`/`LIMIT` pagination.
2. Keyset (cursor) pagination on `(created_at DESC, id DESC)`.
3. Return everything and let the client page.

**Choice.** Option 2, with `limit` bounded to 1–100 (default 25) and a `next_cursor` string.

**Why.** Option 1 has a correctness problem, not just a performance one: if an item is
created between two page requests, every later page shifts by one and the reader sees an item
twice and misses another. Keyset paging resumes from a *position* rather than a row count, so
that cannot happen. The tie-breaker on `id` makes the ordering *total* — without it, two items
created in the same millisecond could be ordered differently per query, and a page could
repeat or skip rows even with no concurrent writes. Option 3 fails the requirement outright.

The cursor is base64url of `(created_at, id)` and is deliberately **opaque but unsigned**: it
carries no secret and no permission decision, so nothing is gained by signing it and nothing
is lost by a client tampering with it. Authorization is re-derived from the row on every
request, and a malformed cursor is a `422` rather than a silent restart from the beginning
(which would quietly repeat a page).

**Trade-offs.**

- Accepted: the cursor cannot jump to an arbitrary page number, and a client that loses its
  cursor starts again. Suits an operational list; a "page 7 of 20" control is not offered.
- Accepted: the expanded `OR`/`AND` cursor predicate is applied as a filter after the index
  seek, so a deeper page walks past the index entries it skips rather than seeking directly
  to its position. Measured and documented in `docs/DATA_MODEL.md`; it remains strictly
  better than `OFFSET`, which would additionally read those rows' heap tuples.
- Rejected: a total row count alongside each page, which costs a full count on every request
  for a number nobody acts on. `has_more` answers the question a client actually asks.

**Consequences and verification.** `tests/test_items_list.py` (15 tests): filters combine and
are applied in the database; a full walk visits every item exactly once; **items created
between page 1 and page 2 neither duplicate nor skip** (the property `OFFSET` cannot offer);
twelve items sharing one `created_at` still page correctly through the `id` tie-breaker;
`limit` `0`, `101` and `-1` are `422` while `100` is accepted; malformed cursors are `422`;
search matches title and description and cannot cross a team boundary; and the list payload
omits `description`, which only the detail view carries.

---

## 14. Index and query decisions come from measurements, not taste

**Context.** The brief asks for indexes based on actual query patterns and forbids claiming
scale that has not been measured. Adding an index is easy and adding the wrong ones is
worse than adding none: they cost write throughput and can be ignored by the planner anyway.

**Choice.** Three practices, applied to the work-item API:

1. The list query is built by a **pure function** (`list_statement`), separate from the code
   that executes it, so the exact statement the API runs can be compiled and `EXPLAIN`ed.
2. `scripts/explain_lists.py` prints the plans for the real query shapes against the seeded
   database. The numbers in `docs/DATA_MODEL.md` come from that script, not from estimates.
3. An index is added only where a measurement shows a plan that would not improve with
   scale, and the measurement is recorded next to the index.

**What the measurements actually changed.**

- **An index was added, and it was needed.** The default team view (no `status` filter) was a
  sequential scan of the whole table plus a top-N sort: 3.19 ms against 0.32 ms for the same
  list narrowed by status. The cause was structural — `ix_work_items_team_status_created`
  leads with `team_id` but its next column is `status`, so an unfiltered list has nothing to
  walk in order and would have stayed a full scan at any size. Revision `0002` adds
  `ix_work_items_team_created`. Measured after: **0.53 ms**.
- **A silent 20× penalty was found and removed.** The text-search configuration was passed to
  `websearch_to_tsquery` as a *bound parameter*. The plan still showed a `Bitmap Index Scan`,
  which looks like success, but the index condition was the function call itself: PostgreSQL
  could not evaluate the tsquery once and instead re-evaluated it **for every index entry**.
  Measured under a forced generic plan: **5.47 ms against 0.245 ms** with the same plan
  shape. `SEARCH_CONFIG` is now a literal `'english'::regconfig` — a fixed internal constant,
  never user input — which lets the tsquery be folded once. The search term itself remains a
  bound parameter.
- **A partial index was justified rather than assumed.** The unassigned-urgent partial index
  is used for the shape it was built for (0.31 ms) and is *not* used when the same queue is
  asked without constraining `status` (0.82 ms via the general team index). Both rows are in
  `docs/DATA_MODEL.md`, because reporting only the flattering one would be misleading.
- **A previously wrong claim was corrected.** An earlier revision of `docs/DATA_MODEL.md`
  stated that full-text search did not use its GIN index at the seeded size. Re-measuring
  against the shape the API actually issues showed that it does. The document was corrected
  to match the plan rather than left alone for consistency.

**Trade-offs.**

- Accepted: one more index to maintain on write, justified by a measured 6× difference on the
  most common read.
- Accepted: the measurement script is committed even though it is not part of the
  application, because an unreproducible number is not evidence.
- Rejected: speculative indexes on every filterable column. `status`, `priority`, `type` and
  `assignee_id` are all queried, but only the combinations with a measured plan got an index;
  the rest are served by filtering rows the existing indexes already narrowed.
- No claim is made about the brief's tens-of-thousands-of-items target. Nothing has been
  load-tested, and `docs/KNOWN_LIMITATIONS.md` says so.

**Consequences and verification.** `uv run python -m scripts.explain_lists` reproduces every
number in `docs/DATA_MODEL.md`. `tests/test_index_inventory.py` asserts each index exists with
the intended definition, including the one added in `0002`.

---

## 15. Idempotency is one shared mechanism, applied to every create

**Context.** Creating a work item is retry-safe: the client sends an `Idempotency-Key`, the
server stores the outcome and replays it. Adding comments raised the same problem — a
double-click or a retry after a timeout must not post the same note twice — and the tempting
move was to copy the item code path and adjust it.

**Options considered.**

1. Copy the item idempotency logic into the comment service.
2. Extract the mechanism into `app/services/idempotency.py` and call it from both.
3. Give comments a weaker guarantee (for example, deduplicate on an exact body match).

**Choice.** Option 2. `fingerprint`, `claim_key`, `replay`, `complete_key` and `validate_key`
live in one module; the unique constraint `(actor_id, operation, key)` on `idempotency_keys`
is what actually decides a race, and both callers share it.

**Why.** A copied mechanism drifts: the second copy gets a bug fix the first does not, and
"retrying is safe" starts meaning different things on different endpoints. Option 3 is not a
guarantee at all — two people legitimately posting the same sentence is a real thing, and a
body hash cannot tell that from a retry. The key is scoped to `(actor, operation)`, so a key
used for a create cannot collide with a key used for a comment by the same person, and
`Operation` is an enum so the set of idempotent operations is visible in one place.

**Trade-offs.**

- Accepted: one more module and an indirection for the item path, in exchange for one place
  where idempotency can be reviewed and one set of tests that covers both.
- Accepted: `Idempotency-Key` is *required* on both creates. An optional key means an
  unguarded request path exists, and the failure mode (a duplicate) is silent.
- Accepted: the key row is written inside the operation's transaction, so a rolled-back
  operation does not burn its key. This is what lets the atomicity test retry successfully
  after a forced failure.

**Consequences and verification.** `tests/test_comments.py` proves the comment path: a replay
returns the same id with `Idempotent-Replay: true`, a reused key with a different body is
`409 idempotency_key_reuse`, and two concurrent identical comments produce one row and one
event. `tests/test_items_create.py` covers the same ground for items against the same module,
and `tests/test_activity_atomicity.py` shows a failed comment leaves no key behind.

---

## 16. Comments are addressed through their item, and the team comes from the item's row

**Context.** A comment belongs to a work item, and a work item belongs to a team. The API
needed a URL and an authorization rule.

**Options considered.**

1. `/teams/{team_id}/items/{item_id}/comments` — the team is in the path.
2. `/items/{item_id}/comments` — the team is derived from the item.
3. A top-level `/comments` collection with `work_item_id` in the body.

**Choice.** Option 2. The route is `/items/{item_id}/comments`, and the service loads the item
and authorizes against **the item's own `team_id`**.

**Why.** Option 1 puts a team id in the URL that the client supplies, which is exactly the
thing the brief says never to trust as proof of access. The server would have to check that the
path's team matches the item's team anyway — and if it forgot to, the path would be a
confused-deputy hole. Deriving the team from the row removes the check rather than adding it:
there is nothing to compare, because there is only one team in play. Option 3 is worse for
reads (every list would need a join or a filter the caller supplies) and makes the item's
thread harder to find.

The rule lives once, in `Principal.require_item_access`, so the comment readers, the activity
readers and the item detail endpoint cannot disagree about it. A non-member gets `404`, the
same answer as for an id that was never issued.

**Trade-offs.**

- Accepted: the URL is nested, which is slightly less REST-conventional than a flat
  `/comments/{id}`. Comments are never addressed by their own id in this API, so the nesting
  costs nothing.
- Accepted: no `team_id` appears anywhere in a comment request, so a client cannot express an
  opinion about which team it is acting in.

**Consequences and verification.** `tests/test_comments.py` and `tests/test_activity_api.py`
each assert that a non-member's response is **byte-identical** to the response for an invented
id, on both reads and writes.

---

## 17. History is readable over HTTP, and never writable

**Context.** Every mutation already writes an `activity_events` row in its own transaction, and
until this phase the only way to read the result was `psql`. The brief asks for reliable
history; a history nobody can see is hard to call reliable.

**Options considered.**

1. Return the timeline inline with the item.
2. A separate, read-only endpoint per scope, paged like every other list.
3. An endpoint that accepts an event (so a client could "correct" history).

**Choice.** Option 2, in two scopes: `/items/{item_id}/activity` and `/teams/{team_id}/activity`.
Option 3 is refused outright — there is no route that writes an event, and the database trigger
on `activity_events` rejects `UPDATE` and `DELETE` regardless of what any future code does.

**Why.** Option 1 ties the size of an item response to the length of its history: an item with
three hundred events would carry three hundred events into a view that shows a summary. It also
forces one cursor to serve two very different result sets. Option 3 would make the audit trail
editable by the very clients it is meant to hold to account.

Reading history requires `VIEW_TEAM`, so a viewer may read it: the timeline names people and
describes changes, but it is the team's own record, and hiding it from the person who is meant
to be able to see the work would be arbitrary. The team feed is built from the event's
denormalised `team_id`, so it needs no join back to `work_items` and cannot be assembled by a
query that forgot the team filter.

**Trade-offs.**

- Accepted: two endpoints and two cursors rather than one.
- Accepted: an extra round trip for a client that wants both the item and its history.
- Accepted: history is never redacted or amended. If an item moves to another team — which the
  API does not allow today — the existing events keep the team they were written with.

**Consequences and verification.** `tests/test_activity_api.py` (13 tests): the timeline is
newest first and carries the stored values; the team feed covers the whole team and no other;
a viewer reads and a non-member gets a `404` identical to an unknown id; a long timeline pages
without duplicates or gaps, including when every row shares a timestamp; and `POST`, `PATCH`,
`DELETE` and `PUT` on either view are `405`.

---

## 18. Each list has its own cursor, and the id kind is what enforces it

**Context.** There are now four paginated lists: team items, a team's activity, an item's
comments and an item's timeline. They all page the same way — keyset on `(created_at, id)` —
so a cursor is structurally interchangeable between them.

**Options considered.**

1. One cursor format, reusable anywhere, treated as a position.
2. One format, with the list's identity encoded in the cursor and validated on decode.
3. One format, with the *id kind* declared per route and validated on decode.

**Choice.** Option 3. Most lists are keyed by a UUID; `activity_events` is keyed by a bigint
identity. `decode_cursor(cursor, id_kind="int")` refuses a UUID-shaped id and vice versa.

**Why.** Option 1 was the first implementation and it produced a real bug, found by the
end-to-end pass rather than by a unit test: an item-list cursor handed to the activity endpoint
decoded fine and then reached SQL as a `bigint < uuid` comparison, which is a `500`. A
position that is *meaningful* in one list and *silently wrong* in another is worse than one
that is refused. Option 2 is more machinery than the problem needs — a list label would have
to be invented and kept in sync with the routes — when the id type already distinguishes the
activity views from everything else, for a real reason (the table's key) rather than a
decorative one. The important property is that a cursor is refused by the wrong list; the id
kind achieves it without a new identifier to maintain.

**Trade-offs.**

- Accepted: the cursor is still unsigned and carries no permission decision — it is a position,
  not a capability. Authorization is re-derived from the row on every request, so a tampered
  cursor can only produce a different valid position within data the caller may already read.
- Accepted: the team item list and an item's comment thread share the UUID kind, so a cursor
  from one is *accepted* by the other and yields a valid but meaningless position. Both are
  UUID-keyed ordered lists, and the caller can only ever see rows they are authorized to read;
  a client that mixes them up gets a short page, not a leak. Documented rather than papered
  over with a label.
- Accepted: one more keyword argument at every decode site, which is where the list's key type
  is now stated explicitly.

**Consequences and verification.** `tests/test_activity_api.py` asserts that an item-list
cursor is `422` on the activity endpoint, and `tests/test_items_list.py` still covers the
malformed-cursor and bounds cases. The end-to-end pass exercises the cross-list case over real
HTTP, and `docs/DATA_MODEL.md` records the plan for each of the four lists.

---

## 19. The interface holds server state, and adoption beats re-fetching

**Context.** The frontend has to display work items, let people claim them, move them through
the workflow and edit them. TanStack Query makes optimistic updates easy, and the brief asks
that "a mutation should return the authoritative server result" and that after a conflict the
client "refresh/reconcile rather than forcing stale local data onto the server".

**Options considered.**

1. Optimistic updates everywhere: apply the change locally, send it, roll back on failure.
2. No local state at all beyond the query cache; every write waits for the server.
3. Optimistic only where the changed state is small and the rollback is unambiguous;
   server-confirmed everywhere else.

**Choice.** Option 3. When this was first written, that meant **every** item write was
server-confirmed, because no case passed the test in option 3. **Decision 24 revisits the
claim**, which now updates optimistically with a tested rollback; transitions and edits remain
server-confirmed. The reasoning below is why they do.

**Why.** Option 1 is a correctness problem before it is a taste problem. Approval and closure
are operations whose *entire point* is that a controlled state is being entered: a transition
that optimistically advances the status and then comes back `412` shows a workflow state that
never existed on the server. That is worse than a button that waits, because it teaches the
user to distrust the screen. A claim is the one case where the rollback is a single field and
the losing path has a clear explanation — which is exactly the test decision 24 applies.
Option 2 would be over-correction for a form: a title edit is not contested by anybody, so
waiting for a round trip costs nothing and is what the code does anyway.

Two related choices follow from this and are worth stating, because they are what make
"server-confirmed" cheap rather than sluggish:

- **A successful write's response is adopted rather than re-fetched.** The response *is* the new
  state, so it goes straight into the item's cache entry. That removes a redundant `GET` and,
  more importantly, means the interface can never show a locally invented result.
- **The `ETag` is stored with the item, under one query key.** The version and the item are one
  fact; keeping them in separate places is precisely how a client ends up conditioning a write
  on yesterday's version while displaying today's data.

A refused write does re-read the item, because then the local copy is *known* to be stale —
which is the opposite of forcing stale local data onto the server.

**Trade-offs.**

- Accepted: a visible round-trip delay on every item write. On a local network it is a few
  milliseconds; the button says what it is doing.
- Accepted: no perceived speed win to demo. The win is that the screen never shows a state the
  server did not confirm.
- Accepted: more cache plumbing — `putItem`, `refreshItem` and `invalidateItemViews` rather than
  one `invalidateQueries` call — because the two cases genuinely differ.

**Consequences and verification.** `ItemDetailPage.test.tsx` asserts that a transition adopts
the version **from the response** (and that no second read was needed), that a `412` says the
change "was not applied" and offers a reload, that a viewer is offered no moves, and — since
decision 24 — that an optimistic claim is provisional, rolls back on `409`, and hands the next
write the version the claim produced. `ItemListPage.test.tsx` asserts that a filter change is a
new server query rather than a filter over loaded rows.

---

## 20. The bearer token is held in memory, and a reload signs the user out

**Context.** The API issues an opaque bearer token. The browser has to keep it somewhere, and
every option is a trade the brief explicitly asks to be documented rather than assumed.

**Options considered.**

1. `localStorage` — survives everything.
2. `sessionStorage` — survives a reload, dies with the tab.
3. A module variable — survives neither.
4. An `HttpOnly` cookie — unreadable by script, but sent automatically by the browser.

**Choice.** Option 3. The token lives in `frontend/src/api/client.ts` and is written nowhere
else.

**Why.** The first thing to say plainly is that **none of 1–3 defends against XSS.** All three
are readable by script running on the origin, so the choice is not "secure or insecure" — it is
how long a stolen token stays usable. `localStorage` persists until explicitly cleared, so a
token captured once survives browser restarts. `sessionStorage` narrows that to the tab.
A module variable narrows it to the open page, and is the only option where closing a tab is
sufficient to destroy the credential.

Option 4 is the genuinely different one: script cannot read an `HttpOnly` cookie. But a cookie
the browser attaches automatically is a CSRF vector, and closing that requires a token,
`SameSite` configuration and care with cross-origin requests. The project chose the weakness it
mitigates with controls it actually implements — no HTML injection anywhere, a strict CSP
documented in `OPERATIONS.md`, short session lifetimes and immediate revocation — over the one
that needs machinery it has not built. That reasoning, including why the alternatives are not
simply "worse", is in `docs/SECURITY.md`.

**Trade-offs.**

- **Accepted, and it is a real cost: a page reload signs the user out.** The session is short
  (12 h absolute, 2 h idle) and the demo accounts are one click away on the sign-in screen, so
  the price is small — but it is a price, not a free win. An application where a lost page means
  lost work should choose `sessionStorage` instead.
- Accepted: no "remember me". There is no long-lived credential to remember.
- Accepted: the interface must handle a `401` from anywhere, which it does in one place (the
  query cache's `onError`).

**Consequences and verification.** `client.test.ts` asserts the token is attached only once a
session exists, and that a `401` is flagged as an authentication failure;
`query/client.ts` turns that flag into a cleared session. `LoginPage.test.tsx` asserts that a
failed sign-in leaves no session behind — including the case where the token is issued but the
memberships cannot be read, which is the case a naive implementation gets wrong.

---

## 21. Retries are opt-in, and only where the server can recognise them

**Context.** TanStack Query retries failed mutations by default. For this API that default is
wrong in one direction and unhelpful in the other.

**Options considered.**

1. Retry all mutations.
2. Retry none.
3. Retry only the operations that carry an `Idempotency-Key`.

**Choice.** Option 3. The default is `retry: 0`, and `CreateItemForm` and `CommentThread` opt in
with `retry: 2`.

**Why.** A retried claim, after the first attempt succeeded, comes back `409` — the item is now
owned. A retried transition comes back `412` — the version moved. Retrying those does not make
the application more reliable; it converts a successful operation into an error message. A
retried create or comment is the opposite: the server holds an `Idempotency-Key` row and replays
the original result, so the retry is *free* and the user never sees the blip.

The subtlety that makes it work is where the key comes from. **It is a mutation variable,
generated once when the user acts** — not inside the request function. TanStack reuses the
variables for every attempt of the same mutation, so every attempt carries the same key and is
the same logical operation. Generating the key inside `mutationFn` would give each retry a fresh
key and turn a network blip into a duplicate work item, which is exactly the failure the API's
required `Idempotency-Key` exists to prevent. This is a one-line difference with opposite
outcomes, so it is asserted directly rather than assumed.

Query retries are narrowed the same way: transport and `5xx` faults retry, `4xx` does not. A
`403` will not become a `200`, and retrying an authentication failure only delays the sign-in
screen.

**Trade-offs.**

- Accepted: a per-mutation override in two places, rather than one global setting.
- Accepted: an automatic retry can still surprise a user who clicked once — but for these two
  operations the second attempt cannot produce a second item, which is the whole point.
- Accepted: no retry for edits, so a dropped connection during an edit is a visible failure the
  user retries by hand. That is honest: the second attempt is a *new* conditional write, and
  pretending otherwise would need a key on PATCH as well.

**Consequences and verification.** `CommentThread.test.tsx` fails the first attempt at the
transport layer and asserts the retry carries **the same** `Idempotency-Key` — the property a
refactor would break silently. `ItemDetailPage.test.tsx` covers the two refusals that make
retrying unsafe.

---

## 22. A hand-written router, and a deliberately small dependency list

**Context.** The application has five views, one of which is nested inside another
(`#/teams/:teamKey/items/:itemId`), and no server-side rendering.

**Options considered.**

1. `react-router-dom`.
2. A hash router of about sixty lines.
3. No routing: a single page with view state in React.

**Choice.** Option 2.

**Why.** A router library earns its place through data loaders, nested layouts, redirects and
route-level code splitting. This application has none of those: there are five views, one
nested route, and the data is fetched by TanStack Query from the item id in the URL. What is
left is "parse the hash and subscribe to changes", which is a pure function plus
`useSyncExternalStore` — and being a pure function, `parseHash` is tested directly, including the
inputs that must be *refused* rather than guessed at (an item id that is not UUID-shaped is a
`404`, not a request with a nonsense id).

Option 3 fails a real requirement: a view has to be shareable and reloadable, which is also why
the team comes from the URL rather than from component state.

Hash routing rather than the History API is the same reasoning applied once more: it needs no
server rewrite rule, so the built `dist/` is servable by any static file server.

**Trade-offs.**

- Accepted: no route-level code splitting, so the whole bundle loads at once. At 89 kB gzipped
  for the entire application, splitting would add a network round trip to save less than the
  latency it costs.
- Accepted: hand-written parsing means hand-written edge cases, which is why the router has its
  own test file covering the shapes that must not match.
- Accepted: the dependency list stays at React, React DOM and TanStack Query at runtime. Every
  other package is a build or test tool.

**Consequences and verification.** `lib/router.test.ts` covers every real route (including the
two person-scoped ones), the landing hash, a non-UUID item id, a truncated path and a path with
a trailing segment. `App.tsx` looks
the team key up in the caller's own memberships and shows "that team is not one of yours" rather
than an empty list, because the server would refuse every request for it anyway.

---

## 23. The outbox is a table, and the worker is a separate process

**Context.** Three moments need to reach a person other than the actor: an item assigned to
them, an item they own approved, and an item they own sent back. The write that causes each
one must not depend on the notification being delivered, and the notification must not be
lost if the process handling the write is restarted a second later.

**Options considered.**

1. Insert the notification inline, in the same request. Simplest, and it cannot be lost — but
   it couples an operational write to a delivery write, and a failure to notify fails the
   assignment.
2. Insert it after the response with `BackgroundTasks`. No separate process, and no new
   infrastructure. It also dies with the process: a deploy in the wrong second silently
   loses the notification, and nothing records that it was owed.
3. A transactional outbox table, drained by a separate worker process.
4. A broker (Redis, RabbitMQ, Celery).

**Choice.** Option 3.

**Why.** The first half of the design is a single transaction: the state change, its activity
event and the outbox row commit together or not at all. That is what makes "the assignment
happened but nobody will ever be told" impossible — the same guarantee the history already
had, extended to delivery. The second half is a process boundary: the API does not deliver,
so a slow or broken notification cannot slow a request, and restarting the API does not stop
delivery.

Option 4 was rejected because the queue already lives in a database that has row locking and
`FOR UPDATE SKIP LOCKED`. A broker would add a second place where the truth lives, a second
thing to run, and a failure mode where the database and the queue disagree. `SKIP LOCKED` is
exactly the primitive a work queue needs, and it is already here.

**At-least-once, and why the effect is still exactly-once.** A worker can claim a batch, do
the work, and crash before committing; the next poll sees the rows again. Rather than pretend
otherwise, delivery is at-least-once and the *handler* is made idempotent by the database:
`notifications.outbox_event_id` is unique, and the insert is `ON CONFLICT DO NOTHING`.
Replaying an event therefore cannot create a second notification, and a handler that forgets
to check is still safe. "Exactly-once delivery" is not claimed, because it is not true;
"exactly-once effect" is, and a test proves it.

**Retries and giving up.** A failed event records its error, increments `attempts`, and is
rescheduled with a bounded backoff (2, 4, 8, 16, 32, capped at 60 seconds). At
`OUTBOX_MAX_ATTEMPTS` (default 5) it becomes `failed`, which is terminal and visible: the
operator summary counts failed events, and the worker logs the last error at `ERROR`. A
poison message therefore stops being retried without disappearing.

**Trade-offs.**

- Accepted: one more process to run. It is a Compose service, a `make worker` target and one
  paragraph in the README, which is the smallest honest cost.
- Accepted: delivery lags the write by up to the poll interval plus processing (about a
  second locally). The notification view says so instead of implying a live feed.
- Accepted: only three event types notify. A notification per status change would make the
  inbox the noisiest view in the product, and the activity feed already covers the rest. This
  is a product judgement, recorded here so it is not mistaken for an omission.
- Rejected: exactly-once delivery. It would need a distributed transaction between the
  database and the delivery target.

**Consequences and verification.** `tests/test_outbox.py` covers: an assignment and its event
committing together; a deliberately broken outbox insert rolling the assignment back; a
delivered notification; replaying an event producing no second row; a departed recipient; two
workers draining one batch without either blocking or double-delivering; retry exhaustion
marking the event `failed`; and a malformed payload failing observably rather than silently.
`tests/test_notifications.py` covers the read side and the team boundary.

---

## 24. Optimistic UI, only where the rollback is unambiguous

**Context.** The interface was server-confirmed everywhere: click, wait, then show. That is
right for a workflow transition and wrong for a claim, where the expected latency is on the
critical path of a race and the losing case has a clear, useful explanation.

**Options considered.**

1. Server-confirmed everywhere.
2. Optimistic everywhere, with rollback helpers shared by all writes.
3. Optimistic where the state changed is one field and the failure has an obvious
   reconciliation; server-confirmed for everything that changes what the item *is*.

**Choice.** Option 3, applied to the claim only.

**Why.** A claim changes exactly one field — the assignee — and the server's answer is either
"yours" or "somebody else's". Showing it immediately is honest as long as the interface says
it is provisional and the losing path replaces the guess with the server's truth. A status
transition is the opposite: `awaiting_approval` that never happened is a workflow state a
person could act on, and approval is the operation the product exists to control. Waiting for
the response there costs one round trip and removes a class of lie.

**Mechanics.** `cancelQueries` for the item, then a snapshot, then `setQueryData` with the
caller as assignee. On success the response body *and its `ETag`* are put back into the same
cache entry; on failure the snapshot is restored, the item is re-read from the server, and the
dependent lists are invalidated. While the mutation is pending, the provisional state is
announced and the transition buttons are disabled, so a stale `available_transitions` cannot
be acted on.

Writing this found a real bug: the claim response carries a **new** `ETag` because claiming
bumps the version, and the client was discarding it. The next transition therefore sent the
pre-claim version and was refused with `412`. The fix is part of this change and is covered by
a test that fails without it.

**Trade-offs.**

- Accepted: a visible flicker on the losing path — the row shows the claimant as owner and
  then corrects to the winner. The alternative is a slower success for the common case, and
  the flicker is accompanied by an explanation rather than a silent change.
- Accepted: a write path that is harder to read than the server-confirmed one. It is confined
  to one mutation and documented at the call site.
- Rejected: a generic optimistic-update helper. With one call site it would be an abstraction
  with no second user.

**Consequences and verification.** `ItemDetailPage.test.tsx` holds the claim response open and
asserts the provisional owner and the "until the server confirms" notice; resolves it with a
`409` and asserts the row rolls back to the winner and the alert explains it; and asserts the
next conditional write carries the `ETag` the claim returned. The last test was run against the
old client to confirm it fails there.

---

## 25. The unread badge polls, and says so

**Context.** Notifications are delivered by a worker process, so the browser cannot know the
moment one arrives. The badge still has to be roughly right.

**Options considered.**

1. WebSockets.
2. Server-sent events.
3. Polling an indexed `COUNT` every 20 seconds.

**Choice.** Option 3.

**Why.** The assessment explicitly rules out WebSockets, and they are the wrong shape anyway:
the traffic is one number, one direction, every so often. Server-sent events would fit, but the
producer is a **different process** from the API, so making it push would require the worker to
signal the API process — `LISTEN/NOTIFY`, or a broker. That is a real feature, not a small one,
and it belongs with the deployment work rather than ahead of it.

Polling needs nothing new: the count is one indexed query (`ix_notifications_recipient_unread`
is partial, so it only covers rows that can be unread), the query key is separate from the list
so other pages do not fetch notifications to render a number, and a 20-second delay on a badge
is not a correctness problem. The notification page states that delivery is asynchronous, so
the lag is visible rather than mysterious.

**Trade-offs.**

- Accepted: up to 20 seconds of staleness, and one request per open tab per interval.
- Accepted: a small amount of avoidable traffic on a page nobody is looking at. TanStack Query
  stops the interval when the tab is in the background by default.
- Rejected for now: SSE. It is the natural next step and is written down as such in the
  README's "next week" list, rather than half-built.

**Consequences and verification.** `useUnreadCount` is one hook with one query key;
`NotificationsPage.test.tsx` asserts the badge and the list can disagree only if the server
says so (both are re-read after an acknowledgement). The partial index is asserted by
`tests/test_index_inventory.py`.

---

## 26. Saved views are server filters, not a client-side pass

**Context.** The brief names three operational questions — assigned to me, unassigned urgent,
awaiting approval. They have to work over the whole dataset, not over whatever page happens to
be loaded.

**Options considered.**

1. Filter the loaded rows in the browser.
2. A preset button that sets the existing structured filters, which the list already sends to
   the server.
3. Dedicated endpoints per view.

**Choice.** Option 2.

**Why.** The list endpoint already filters, searches and pages on the server, and its cursor is
defined over the filtered set. A preset is therefore a *value*, not a code path: one click sets
the same state the checkboxes set, and the request that follows is the request the user could
have built by hand. Option 1 would answer the wrong question — "of the 25 rows you have, which
are urgent?" — and option 3 would duplicate paging, filtering and authorization for no new
capability.

The "unassigned urgent" preset is the one that proves the point: it sets `assignee=none`,
`status=open` and `priority=high,urgent` together, which is exactly the shape the partial index
`ix_work_items_unassigned_urgent` exists for. A client-side version would never exercise it.

**Trade-offs.**

- Accepted: the selected preset is component state, so a reload returns to "all work". Making
  it a URL parameter would make the view shareable and is the obvious next step; it also means
  the router grows query-string handling, which is deliberately absent today.
- Accepted: four preset buttons and a filter bar can describe the same state. The active preset
  is derived from the structured filters (search is excluded), so the highlight cannot drift
  from what is actually being requested.

**Consequences and verification.** `ItemListPage.test.tsx` asserts each preset becomes the
expected query parameters, that "all work" returns to an unfiltered request, and that typing a
search term does not clear the selected preset. `MyWorkPage.test.tsx` asserts the cross-team
view sends no `assignee` parameter at all.

---

## 27. One command runs the whole stack

**Context.** A reviewer should be able to see the application without reading a setup guide,
and the project now has four moving parts: the database, the API, the worker and the web app.

**Options considered.**

1. Documentation only: four terminals and a list of commands.
2. Development scripts that start four processes on the host.
3. A Compose file with four services and two images.

**Choice.** Option 3, with the step-by-step path kept for anyone who wants to run the tests or
the dev server.

**Why.** One command that cannot half-succeed is worth more to a reviewer than a shorter
document. Compose also expresses the startup order the system actually needs: the database must
be healthy before migrations, the API must be ready before the worker claims rows, and the web
container only needs the API to exist. Those are dependencies, not an arbitrary sequence.

The web container serves the built SPA and forwards `/api` to the API container, so the browser
uses **one origin**. That removes CORS from the deployment entirely and means the image has no
host name baked into it: `VITE_API_BASE_URL` is built as an empty string, which the client reads
as "same origin". A reviewer running it on a laptop and a deployment behind a proxy configure
the same thing.

Migrations run in the entrypoint rather than at application startup. A web process that creates
tables as a side effect of booting is the anti-pattern this project keeps out of the request
path; making the step explicit also lets the healthcheck wait for it.

**Trade-offs.**

- Accepted: the images must be built, which needs Docker and a couple of minutes the first time.
- Accepted: no hot reload inside the containers. This is the *review* path, not the development
  path; step 9 of the README is the development path.
- Accepted: development-grade configuration — a published database port and the documented demo
  password. It is explicitly not a production deployment, and the README says so.
- Accepted: the worker shares the API image and differs only in its command. Two images would
  duplicate the build for no benefit.

**Consequences and verification.** `docker compose up --build` was run, and 26 checks were made
over real HTTP through the nginx origin: the proxied health endpoints, login, claim (including
the new `ETag`), assignment, worker delivery, the notification list and unread count,
acknowledgement, `/me/items`, a cross-team `404`, a stale `412`, and the activity feed. All 26
passed. The exact commands and results are in
[`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

---

## Decisions taken, not yet implemented

Listed so the direction is explicit without implying that the behaviour exists.

| Decision | Direction | Implemented in |
| --- | --- | --- |
| Outbox delivery over email or a webhook | Reuse the same queue, event types and exactly-once constraint; add a provider retry policy and an operator view of permanently failed deliveries | The in-app channel is complete; the second channel is not started |
| Push instead of polling | Server-sent events, with the worker signalling the API process through `LISTEN/NOTIFY` | The badge polls; see decision 25 |
| Deliberately not built | Attachments, SSO, WebSockets, mobile, comment editing, reopen-after-close, rate limiting, member-management UI | See `docs/KNOWN_LIMITATIONS.md` |

