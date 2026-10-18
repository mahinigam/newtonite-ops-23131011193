# Implementation Status

A truthful, phase-by-phase record. "Verified" means the listed command was run and its
result observed in this repository — not that the code looks correct.

**Branch:** `feature/newtonite-operations-platform`, merged to `main`; Phase 7–8 work on
`feature/outbox-notifications`
**Last updated:** 2026-10-18 (end of Phase 8)

## Phase summary

| Phase | Scope | Status | Verified by |
| --- | --- | --- | --- |
| 0 | Reconnaissance and design | Complete | Repository inspected, remote confirmed empty, brief and execution plan read |
| 1 | Runnable foundation | Complete | See below |
| 2 | Domain, seed data and constraint tests | Complete | See below |
| 3 | Authentication and team authorization | Complete | See below |
| 4 | Work-item API and the five critical correctness behaviours | Complete | See below |
| 5 | Comments API and activity history API | Complete | See below |
| 6 | Frontend vertical slice | Complete | See below |
| 7 | Transactional outbox, worker, notifications and `/me/items` | Complete | 229 backend and 79 frontend tests; see below |
| 8 | Documentation, the container stack and reviewer readiness | Complete | `make check`, `docker compose up --build`, 26/26 HTTP checks; see below |

## Phase 0 — Reconnaissance and design

**Requirements.** Inspect the repository and remote before changing anything; read
`AGENTS.md`, the test-and-fix skill, the official challenge brief and the university
execution plan; record assumptions, risks and planned architecture.

**Findings.**

- The working directory was a Git repository with **no commits** and only two untracked
  entries: `AGENTS.md` and `.opencode/skills/test-and-fix/SKILL.md`. No existing
  implementation was present, so nothing had to be preserved or reconciled.
- `origin` is `https://github.com/mahinigam/newtonite-ops-23131011193`. The remote existed, was public and
  contained **no refs** (`git ls-remote` returned nothing; the GitHub API reported the
  repository).
- Both source documents were read locally from `~/Downloads`:
  `Newtonite_Software_Engineering_Challenge.pdf` (6 pages) and
  `GU_CCIO_Advisory_Newtonite_Software_Engineering_Execution_Plan (1).pdf` (8 pages).
  They are not part of the repository.
- Tooling present: Python 3.14 (with 3.12 available), `uv` 0.11.16, Node 25, Docker CLI
  (daemon stopped), PostgreSQL 16.13 via Homebrew (stopped), `gh` (unauthenticated at the
  time of inspection).

**Acceptance criteria.** No existing work lost; plan evidence-based. **Met.**

## Phase 1 — Runnable foundation

**Requirements.** Frontend and backend structure, pinned runtime dependencies,
PostgreSQL integration, environment configuration, migrations and seed mechanism, health
checks, Docker Compose, a first smoke test, and documented local startup.

**Acceptance criteria.** The foundation starts, migrations work, the health check
succeeds, and the smoke test passes from a clean setup.

**What was built.**

- `backend/` — FastAPI application factory, environment-driven settings, async SQLAlchemy
  engine and session management, structured `uv` project pinned to Python 3.12 with a
  committed `uv.lock`.
- `alembic/` — async migration environment; revision `0001_initial_schema` creates users,
  teams, memberships, work items, comments, activity events, auth sessions and
  idempotency keys, with their constraints, indexes and the append-only trigger.
- `docker-compose.yml` — PostgreSQL 16 on host port 5433 with an init script that creates
  the test database, so Compose cannot collide with a local PostgreSQL on 5432.
- `Makefile`, `.env.example`, `frontend/.env.example`, `.gitignore`.
- `.github/workflows/ci.yml` — formatting, lint, strict type check and tests against a
  `postgres:16` service container.
- `docker/initdb/01-test-database.sql`.

**Seed mechanism.** Not included in this phase: `alembic upgrade head` is the only setup
step required, and `make seed` does not exist until the seed script does. This was a
deliberate change from the original phase plan (which grouped seeding into Phase 1) to
avoid documenting a command that cannot run. Seeding lands in Phase 2.

**Verification.**

| Check | Command | Result |
| --- | --- | --- |
| Dependencies install from the lockfile | `uv sync --group dev` | Passed |
| Migrations apply to an empty database | `uv run alembic upgrade head` | Passed — `Running upgrade -> 0001, initial schema` |
| Migrations revert cleanly | `uv run alembic downgrade base` then `upgrade head` | Passed on both databases |
| Models match migrations | `uv run alembic check` | Passed — `No new upgrade operations detected` |
| Backend tests | `uv run pytest -q` | Passed — 12 passed |
| Formatting | `uv run ruff format --check .` | Passed — 26 files already formatted |
| Lint | `uv run ruff check .` | Passed — all checks passed |
| Strict types | `uv run mypy .` | Passed — no issues in 25 source files |
| Health endpoints | `tests/test_health.py` | Passed |
| CI (GitHub Actions) | run `38965759133`, job `Backend (lint, types, tests)` | Passed in 32s |

**Commits.** `a9d3e89` (foundation, pushed to `main` and this branch), `530b573`,
`937c7d1`, `aaedb7f` (CI corrections). Two CI fixes were needed and are recorded rather
than hidden: `setup-uv` publishes exact tags only, so `@v10` did not resolve and had to be
pinned to `v10.3.0`; and `ubuntu-latest` is scheduled to move to Ubuntu 26 on
19 October 2026, inside the delivery window, so the runner image is pinned.

**Initial history.** `main` had no commits, so a documented decision was made to create
the foundation as the first commit on `main` (rather than leaving the default branch
empty while work happened elsewhere), then continue on
`feature/newtonite-operations-platform`. Both branches point at the foundation today, and
the feature branch is merged back at the end. No history was rewritten and no force push
was used.

**Invariants proven at the database layer** (`tests/test_schema_constraints.py`), by
issuing raw SQL rather than going through application code:

- An assignee who is not a member of the item's team is rejected by the composite foreign
  key `work_items(team_id, assignee_id) -> memberships(team_id, user_id)`.
- An unassigned item is still allowed (MATCH SIMPLE skips the check when `assignee_id` is
  null).
- `activity_events` refuses `UPDATE` and `DELETE` via a trigger.
- Title length, status value and one-role-per-user-per-team constraints all hold.

**Corrected during this phase.** The first schema draft carried two unique constraints on
the same membership column pair. One was redundant, and the redundancy surfaced as a
confusing test failure. The constraint on `(team_id, user_id)` is retained because it
both enforces "one role per user per team" and serves as the composite foreign key target;
the second became a plain index on `user_id` for per-user lookups. The migration was
amended rather than adding a revision, because revision `0001` had never been released.
Both local databases were rebuilt from scratch afterwards (they contained no data).

**Remaining gaps in this phase.** No frontend yet; no seed data; no authentication; CI has
not yet run on the remote (first push happens with this phase).

**Known defects.** None known.

## Phase 2 — Domain, integrity and demo data

**Requirements.** A complete data model, seed data that makes the demo meaningful at
scale, and tests that prove the domain rules rather than the happy path.

**Acceptance criteria.** Migrations create the schema; the seed produces a coherent,
reproducible dataset; database-level invariants are proven by tests that bypass the
application; indexes match the query patterns.

**What was built.**

- `backend/scripts/seed.py` — a deterministic, idempotent seed: 4 teams, 12 users with
  documented demo credentials, 3 000 work items by default, plus coherent comments and
  activity history.
- `backend/app/security/passwords.py` — Argon2id hashing, plus a constant-time path for
  the "account does not exist" login case.
- `backend/tests/test_seed.py` — 10 tests covering determinism, idempotency and data
  coherence.
- `backend/tests/test_index_inventory.py` — 16 tests asserting every index and named
  constraint exists with the intended shape.
- `docs/DATA_MODEL.md` — the data model, the rule-to-mechanism table, the index rationale,
  and measured query plans.
- A `make seed` target, and CI steps that migrate and seed a clean database before testing.

**Design details worth recording.**

- *Determinism.* Identifiers come from `uuid5` over a fixed namespace, and each item is
  generated from its own seeded RNG, so the dataset does not change if `--items` changes.
  Timestamps are relative to run time so the dashboard looks current; a test asserts that
  structure (not timestamps) is reproducible.
- *Idempotency of history.* Items are inserted with `ON CONFLICT DO NOTHING`, and activity
  is generated **only for rows actually created**. This is what makes a second run add no
  duplicate history — the failure mode that would otherwise make the audit trail
  untrustworthy.
- *Coherent generated states.* Status and assignment are chosen together, so the seed never
  produces an item the workflow would not allow (an unassigned item awaiting approval).
  Open items are unassigned often enough to make the unassigned-urgent queue meaningful.
- *Honest seeding semantics.* The seed is additive, not converging. Documented in the
  script, the README and `KNOWN_LIMITATIONS.md`, because silently rewriting existing rows
  would contradict the append-only history rule.

**Verification.**

| Check | Command | Result |
| --- | --- | --- |
| Seed on a clean database | `uv run python -m scripts.seed --items 3000` | Passed — 4 teams, 12 users, 14 memberships, 3 000 items, 1 878 comments, 11 356 events in ~1 s |
| Seed is idempotent | `uv run python -m scripts.seed --items 3000` (second run) | Passed — **0 rows** created in every table |
| Backend tests | `uv run pytest -q` | Passed — **33 passed** (was 12) |
| Formatting / lint / types | `ruff format --check`, `ruff check`, `mypy .` | Passed |
| Index plans at 3 000 items | `EXPLAIN (ANALYZE, BUFFERS)` on the five hot queries | See `docs/DATA_MODEL.md` |
| CI (GitHub Actions) | run `38966610036` | Passed — every step green, including *Migrate the development database* and *Seed a small dataset* |

**Commit.** `384fe6a` on `feature/newtonite-operations-platform`. No history was rewritten.

**Measured query behaviour** (3 000 items, after `ANALYZE`): team list 0.18 ms via
`ix_work_items_team_status_created`; "assigned to me" 0.18 ms via
`ix_work_items_assignee_created`; unassigned-urgent 0.66 ms via the partial index; a
deep keyset page using an index-only scan with **0 heap fetches**, 0.39 ms; a single
item's timeline 0.06 ms.

**A finding that is documented rather than hidden.** Full-text search does *not* use its
GIN index at this dataset size: with ~3 000 rows the planner prefers a sequential scan and
is right to. Forcing the index shows it is correct and about 4× faster in isolation. The
claims in the documentation were adjusted to match the measurement: the index is described
as built for search and used when the table is large enough, not as universally faster.

**Also corrected during this phase.** The first draft of the batch insert failed because
SQLAlchemy compiles a bulk insert once against the first row's keys, and the `created`
event omitted `from_value`. Fixed with an explicit event factory so every row carries the
same columns — the kind of bug that would otherwise surface as silent data loss.

**Remaining gaps.** No API endpoints yet, so the seeded data is only reachable through
`psql`. No authentication. No frontend.

**Known defects.** None known.

## Phase 3 — Authentication and team authorization

**Requirements.** Real authentication (not a placeholder), authorization decided on the
server for every protected action, and no client-supplied identity or role ever being trusted.

**Acceptance criteria.** A login issues a credential that expires and can be revoked;
credentials are stored so that a database disclosure does not yield usable sessions;
every team-scoped endpoint authorizes against stored membership; a caller outside a team
cannot confirm that the team's data exists; role restrictions are proven by tests that
attempt the forbidden action.

**What was built.**

- `app/security/tokens.py` — 256-bit opaque tokens, stored only as a SHA-256 digest.
- `app/services/auth.py` — login, session resolution, revocation, session housekeeping.
  Absolute and idle expiry, both evaluated with the database clock.
- `app/policy/roles.py` — the permission matrix, defined once.
- `app/policy/principal.py` — the authenticated caller, carrying their role per team.
- `app/api/deps.py` — `get_principal` and `require_team_permission(...)` dependencies.
- `app/errors.py` — one error envelope with stable codes, installed on the app.
- `app/api/v1/` — `auth` (login, logout, me), `teams` (list, roster, set role, remove
  member) and `users` (scoped directory) routers.
- `scripts/maintenance.py` — operator commands to revoke one account's sessions and purge
  long-expired ones.
- `tests/test_auth.py` (15), `tests/test_authorization.py` (20),
  `tests/test_session_housekeeping.py` (5).

**Design details worth recording.**

- *The token is never stored.* The raw token appears only in the login response. A test reads
  every column of `auth_sessions` and asserts the token appears in none of them.
- *Two lifetime rules.* Absolute (12 h from login) and idle (2 h since last use). Every
  comparison uses PostgreSQL's `now()`, so two API processes cannot disagree; `login` also
  derives `expires_at` from the database clock.
- *Reads do not become writes.* `last_used_at` is refreshed at most once a minute, with the
  condition in the `WHERE` clause. Documented, with its one-minute slack, rather than hidden.
- *Failed logins do not leak which addresses have accounts.* Unknown email, wrong password and
  deactivated account return byte-identical `401 invalid_credentials` responses, and the
  unknown-email path spends a real Argon2id verification so the timing matches. A test asserts
  the bodies are equal.
- *404 rather than 403 across a team boundary.* A caller outside a team gets the same answer
  for a real team id and for a random UUID; a test asserts the two responses are identical.
- *Authorization is a dependency.* A route that needs a permission declares it, so it is not
  possible to perform the work before checking.
- *Roles are advisory to the UI, authoritative on the server.* `/auth/me` returns the caller's
  roles so the interface can decide what to show, and every write is checked again from stored
  memberships.
- *Two invariants that protect a team.* A team keeps at least one admin, and a member who is
  still the assignee of work items cannot be removed — the composite foreign key would refuse
  it anyway, but the API turns that into an instruction ("reassign their work first") instead
  of a raw integrity error. A test reassigns the item and asserts the removal then succeeds.
- *Removing a member does not touch history.* Removing a membership leaves `activity_events`
  exactly as it was; a test counts them before and after.

**Verification.**

| Check | Command | Result |
| --- | --- | --- |
| Backend tests | `uv run pytest -q` | Passed — **68 passed** (was 33) |
| Formatting / lint / types | `ruff format --check`, `ruff check`, `mypy .` | Passed — 50 files formatted, 49 files type-checked |
| End-to-end against the seeded dev database | `uvicorn`, then `curl` login → `/auth/me`, `/teams`, rosters, refusals | Passed — see below |
| CI (GitHub Actions) | run `38968414101` | Passed — every step green, including the 73-test suite |

**End-to-end evidence against the real seeded dataset** (3 000 items, 12 demo accounts, real
Argon2id hashes, the documented password):

- `sam.multi@newtonite.dev` signed in and `/auth/me` returned **two memberships with different
  roles**: `platform: member` and `finance: viewer`.
- A wrong password and an unknown email returned the **same** body:
  `{"error":{"code":"invalid_credentials","message":"Email or password is incorrect."}}`.
- `vic.viewer` (viewer) read the platform roster: `200`.
- `sam.multi` (member) and `lena.lead` (lead) each attempted to change a role: `403 forbidden`.
- `sam.multi` (viewer **in** finance) attempted the same in finance: `403`.
- `ada.admin` (admin of platform, **not** a member of finance) attempted it in finance: `404`.
  Same request, different boundary — this is the 403/404 distinction working.
- An invalid role value: `422`, with the field named in `details`.
- After every refusal, the roster was re-read and was **unchanged**.
- The directory returned 8 people for `sam.multi` — the union of platform and finance, and
  nobody from the teams they are not in.

**Commit.** `16d1b59` on `feature/newtonite-operations-platform`. No history was rewritten.

**Remaining gaps.** No work-item endpoints yet, so the seeded items are still only reachable
through `psql`. No frontend. No login rate limiting (documented in
[`SECURITY.md`](SECURITY.md) with the residual risk).

**Known defects.** None known.

## Phase 4 — Work-item API and the five critical correctness behaviours

**Requirements.** Work-item create, read, update and list endpoints; server-side filtering,
search and cursor pagination; and the behaviours the brief singles out — an atomic claim, a
stale update being rejected rather than silently applied, idempotent creation, server-enforced
workflow transitions with role authorization, and an activity history that cannot disagree
with the state it describes. Plus indexing driven by real query patterns, and evidence
rather than assertion for all of it.

**Acceptance criteria.** Two concurrent claims produce at most one claimant; a stale update
is rejected and changes nothing; a replayed create produces at most one item and a mismatched
payload is refused; cross-team access and forbidden role actions are denied; illegal workflow
transitions are refused; a failed history insert rolls the state change back; and every list
view is filtered and paged in PostgreSQL with a bounded page size.

**What was built.**

- `app/services/work_items.py` — the whole item lifecycle. `list_statement` (pure, so the
  exact SQL can be measured), `create_item` (idempotency in one transaction),
  `claim_item` (one conditional `UPDATE`), `update_item`, `transition_item`, and
  `load_visible_item` (authorizes from the row's team).
- `app/policy/workflow.py` — the transition table: origin, destination, required permission,
  `requires_assignee`, and `available_transitions(status, role, has_assignee)`.
- `app/services/activity.py` — transactional history writes, sharing the caller's session.
- `app/services/cursors.py` — `(created_at, id)` cursors, base64url, opaque.
- `app/api/preconditions.py` — `ETag` construction and `If-Match` parsing (`7`, `"7"`,
  `W/"7"`; absent → `428`).
- `app/schemas/work_items.py` — summary and detail shapes, tri-state edit payload.
- `app/api/v1/items.py` — six routes.
- `alembic/versions/0002_team_created_index.py` — one index, added because of a measurement.
- `scripts/explain_lists.py` — the measuring instrument behind `docs/DATA_MODEL.md`.
- Seven new test files: `test_items_create.py` (17), `test_transitions.py` (16),
  `test_items_update.py` (15), `test_items_list.py` (15), `test_claim.py` (9),
  `test_items_concurrency.py` (4), `test_activity_atomicity.py` (4).

**Design details worth recording.** The reasoning is in `ENGINEERING_DECISIONS.md` decisions
9–14; the short version:

- *Claiming is one `UPDATE` whose affected-row count is the answer.* The eligibility condition
  is inside the statement, so there is no read-then-write window to race. A version
  precondition was considered and rejected as the *weaker* guard — it says nothing about the
  assignee.
- *The `Idempotency-Key` is required, and the key row shares the item's transaction.*
  Concurrent duplicates block on the unique index and then replay, so the loser cannot slip
  a second item in.
- *`If-Match` is required on every edit and transition (`428` when absent), and the comparison
  shares the `UPDATE` that bumps the version.* A stale attempt gets `412` with
  `details.current`.
- *Status is not a patchable field.* `PATCH` refuses it, so closure and approval cannot be
  reached by a generic edit.
- *History is written in the same transaction.* Verified by breaking it on purpose.

**Verification.**

| Check | Command | Result |
| --- | --- | --- |
| Backend tests | `uv run pytest -q` | Passed — **154 passed** in ~33 s (was 73) |
| Two concurrent claims | `tests/test_items_concurrency.py` | Passed — `200`/`409`, one assignee, one `claimed` event. Repeated 10× for flakiness, then checked against a deliberately broken implementation |
| Concurrent creates with one key | same file | Passed — `201`/`200`, same id, exactly one row |
| Stale update rejected | `tests/test_items_update.py` | Passed — `412`, `details.current`, newer value intact, no history written |
| Illegal transition refused | `tests/test_transitions.py` | Passed — all edges, plus role denials, terminal `closed`, and two concurrent approvals |
| Failed history insert rolls back | `tests/test_activity_atomicity.py` | Passed — item byte-identical after a forced trigger failure, for edit, claim and transition |
| Formatting / lint / types | `ruff format --check`, `ruff check`, `mypy .` | Passed — 66 files formatted, 66 type-checked |
| Models match migrations | `uv run alembic check` | Passed — `No new upgrade operations detected` |
| Query plans | `uv run python -m scripts.explain_lists` | All seven shapes use an index; see `docs/DATA_MODEL.md` |
| End-to-end over HTTP | `curl` against the seeded database on a real `uvicorn` process | **49/49 checks passed** — see below |
| CI (GitHub Actions) | run `38973882659` | Passed — every step green, including the 154-test suite against a `postgres:16` service container |

**End-to-end evidence against the real seeded dataset** (3 000 items, 12 accounts, real
Argon2id logins, real HTTP, a real server process):

- *Idempotency.* A create with a fresh key returned `201`; the identical retry returned `200`
  with the same id and `Idempotent-Replay: true`; the same key with a different body returned
  `409 idempotency_key_reuse`; no key at all returned `400 idempotency_key_required`.
- *Claiming.* A viewer was refused with `403`; a member claimed the item, which set the
  assignee, bumped the version to 2, left the status at `open`, and wrote one `claimed` event.
- *Concurrency, over real HTTP.* Two members POSTed a claim at the same instant: one `200`,
  one `409`, and **exactly one** `claimed` event in the database. The same create sent twice
  concurrently returned `200` and `201` with the same id, and exactly **one** row existed with
  that title.
- *Stale writes.* An edit with no `If-Match` → `428 precondition_required`; an edit from
  version 1 after the item had moved to version 2 → `412 stale_version` with
  `details.current.version = 2`; the same edit from the current version → `200`, version 3.
- *The workflow.* member claims → starts → submits; the member's attempt to approve was `403`
  and their `available_transitions` was empty, while the lead's held exactly the two approval
  moves; the lead approved, then closed, after which `available_transitions` was empty; an
  attempt to reopen a closed item was `409 invalid_transition`.
- *History.* The item's timeline read back from the database as eight correctly attributed
  events — `created`, `claimed`, `title_changed`, `priority_changed`, `status_changed`,
  `status_changed`, `approved`, `status_changed` — with the two approvals attributed to the
  lead and the rest to the member.
- *Listing.* A page was bounded by `limit` and reported `has_more`; page 2 shared **zero** ids
  with page 1; `limit=101` was `422`; full-text search found the item just created; combined
  filters returned only matching rows.
- *The team boundary.* `sam.multi` (viewer in finance) could read a finance item, while the
  platform lead got `404` for the same item on both read and edit, and `sam.multi` was refused
  `403` for claiming in finance where they are only a viewer.
- *Assignee eligibility.* A lead assigning an item to a member of another team got
  `422 invalid_assignee` on both create and edit; a member assigning to a colleague got
  `403 forbidden` naming `assign_others`; after all three refusals the item was unchanged
  (assignee `null`, version 1).

**A correction recorded rather than quietly fixed.** The first draft of the concurrency test
file called `raw_item` without `await` inside a subscript, which raised
`TypeError: 'coroutine' object is not subscriptable` in four tests. The fix was in the test,
not the assertion. Separately, one test asserted the *order* of field-change events from an
assumed order; the implementation emits them in the field-declaration order, which is
deterministic. The test was corrected to the real, documented order — and the ordering
guarantee is now stated in the code comment on `_FIELD_EVENTS`, so it is a documented
property rather than an accident of `dict` iteration.

**How the concurrency and atomicity tests were validated.** A test that passes proves
nothing if it would also pass against the bug it targets. Each of the three load-bearing
tests was therefore checked by deliberately breaking the implementation:

| Test | Deliberate break | Result |
| --- | --- | --- |
| Concurrent claims | `claim_item` rewritten as read-then-write with a widened window | Both claim tests **failed**; all ten racers "won" |
| Concurrent create | duplicate keys made to proceed instead of replaying | The idempotency test **failed** |
| Activity atomicity | the state change committed before the history insert | The rollback test **failed** with its own message ("the claim survived without its history entry") |

`app/services/work_items.py` was restored after each and verified byte-identical with `diff`.

**A measurement that found real work.** The unfiltered team list was a sequential scan plus a
top-N sort (3.19 ms) because the existing composite index leads with `status`. Revision `0002`
adds `ix_work_items_team_created`; the plan became an index scan at 0.53 ms. Separately, the
text-search configuration was being passed as a bound parameter, which under a generic plan
made PostgreSQL re-evaluate `websearch_to_tsquery` for every index entry — 5.47 ms against
0.245 ms with the same plan shape. Both are documented with their numbers in
`docs/DATA_MODEL.md`.

**Commit.** `545c2cb` on `feature/newtonite-operations-platform`. No history was rewritten.

**Remaining gaps.** Comments and the activity read API do not exist yet, so history is written
and inspectable only through `psql`. No frontend. Reopening a closed item is refused by
design. The verification pass added six probe work items to the development database
(3 006 items, 11 377 events in total); they are ordinary rows and the seed remains additive
and idempotent.

**Known defects.** None known.

## Phase 5 — Comments and the activity read API

**Requirements.** The two things the seeded dataset contains but the API could not reach:
comments on a work item, and the history that every mutation already writes. Both must be
authorized against the item's team, both must paginate server-side with their own cursor, and
history must remain append-only — readable, never writable, over HTTP.

**Acceptance criteria.** A comment and its `commented` event cannot exist apart; a retried
comment produces one comment and a reused key with a different body is refused; a viewer can
read a discussion but not add to it; a non-member gets the same answer for a real item and an
invented id; a long thread and a long timeline each page without duplicates or gaps, including
when every row shares a timestamp; the activity cursor is independent of the item list's; and
no route writes an event.

**What was built.**

- `app/services/comments.py` — `create_comment` (idempotency key, comment and `commented`
  event in one transaction) and `list_comments` (newest first, keyset).
- `app/services/activity.py` — read side added: `item_activity_statement`,
  `team_activity_statement`, `list_item_activity`, `list_team_activity`. The write side
  (`record`) is unchanged and still shares the caller's transaction.
- `app/services/idempotency.py` — the create-item mechanism, extracted so comments reuse it
  rather than growing a second copy: `fingerprint`, `claim_key`, `replay`, `complete_key`,
  `validate_key`. `KEY_MAX_LENGTH` moved next to the column that enforces it.
- `app/services/pagination.py` — page bounds, `next_cursor`, `cursor_for` and
  `descending_keyset`, extracted from the item list so all four lists page identically.
- `app/schemas/pagination.py` — one PEP 695 generic `Page[T]`, so `WorkItemPage`,
  `CommentPage` and `ActivityPage` cannot drift apart.
- `app/policy/principal.py` — `require_item_access`, the single place the "team comes from the
  item's row" rule lives, used by the comment and activity readers.
- `app/services/cursors.py` — cursors now carry a UUID **or** a bigint id, with the kind
  declared at each call site.
- `app/schemas/comments.py`, `app/schemas/activity.py`, `app/api/v1/comments.py`,
  `app/api/v1/activity.py` — four routes: `POST|GET /items/{item_id}/comments`,
  `GET /items/{item_id}/activity`, `GET /teams/{team_id}/activity`.
- Three new/extended test files: `test_comments.py` (18), `test_activity_api.py` (13),
  `test_activity_atomicity.py` (4 → 5).

**Design details worth recording.** The reasoning is in `ENGINEERING_DECISIONS.md` decisions
15–18; the short version:

- *Comments are addressed through their item, and the team is derived from the item's row.*
  There is no team id in the URL for a comment, so there is nothing for a client to lie about.
- *Idempotency is a shared mechanism, not a special case.* The comment path reuses the item
  path's key claim, fingerprint and replay, so "retrying is safe" means the same thing on both.
- *Reading history is `VIEW_TEAM`; a viewer may read.* The timeline names people and describes
  changes, but it is the team's own record.
- *The activity cursor is its own.* Activity rows are keyed by a bigint identity while the other
  lists are keyed by a UUID, and the id kind is declared per route — so a cursor from the item
  list handed to the timeline is a `422` rather than a silently meaningless position.
- *Commenting on a closed item is allowed.* Closure ends the workflow, not the discussion; a
  comment changes no field of the item, so it does not bump `version`.
- *The `commented` event carries a bounded preview, not the body.* `meta.preview` is the first
  200 characters with whitespace collapsed; the comment itself keeps the full text. `meta` is
  rendering help and a client must work without it.

**Verification.**

| Check | Command | Result |
| --- | --- | --- |
| Backend tests | `uv run pytest -q` | Passed — **186 passed** in ~41 s (was 154) |
| Comment atomicity | `tests/test_activity_atomicity.py` | Passed — a forced history-insert failure leaves **no comment** and does not burn the idempotency key; the retry then succeeds |
| Concurrent identical comments | `tests/test_comments.py` | Passed — `200`/`201`, one comment id, one `commented` event |
| Thread and timeline paging | `tests/test_comments.py`, `tests/test_activity_api.py` | Passed — walked page by page, no duplicates, no gaps; a separate case writes every row in one transaction so the `id` tie-breaker is doing the work, and asserts the timestamps really are equal |
| Cursor scope | `tests/test_activity_api.py` | Passed — an item-list cursor is `422` on the activity endpoint |
| Activity is read-only | `tests/test_activity_api.py` | Passed — `POST`/`PATCH`/`DELETE`/`PUT` all `405` |
| Formatting / lint / types | `ruff format --check`, `ruff check`, `mypy .` | Passed — 78 files formatted, 76 files type-checked |
| Models match migrations | `uv run alembic check` | Passed — `No new upgrade operations detected` (no new migration: the comment and activity indexes already existed) |
| Query plans | `uv run python -m scripts.explain_lists` | Passed — 12 shapes, every one using an index; see `docs/DATA_MODEL.md` |
| End-to-end over HTTP | `curl`-equivalent checks against the seeded database on a real `uvicorn` process | **67/67 checks passed** — see below |
| CI (GitHub Actions) | run `38977790902` | Passed — every step green, including the 186-test suite against a `postgres:16` service container |

**End-to-end evidence against the real seeded dataset** (real Argon2id logins, real HTTP, a real
server process, the seeded platform team):

- *Creating a comment.* `201`, the body stored trimmed, the author taken from the session, and
  the item id from the path.
- *Idempotency.* The identical retry returned `200` with the same id and
  `Idempotent-Replay: true`; the same key with a different body returned
  `409 idempotency_key_reuse`; no key returned `400 idempotency_key_required`; a body of spaces
  and a 4 001-character body each returned `422` naming the `body` field.
- *Roles and boundaries.* A viewer was refused `403` for writing and allowed `200` for reading;
  a lead from another team got `404` for both reading and writing, **byte-identical** to the
  response for an id that was never issued.
- *Ordering and paging.* The thread came back newest first; `limit=1` produced a `has_more`
  page whose successor shared **zero** ids with it; `limit=101` and a garbage cursor were each
  `422`.
- *The timeline.* Newest first, ending at `created`; every event named its item and its actor;
  the `commented` events referenced exactly the thread's comment ids and carried a preview, and
  changed no field of the item.
- *The team feed.* Bounded, `has_more`, ordered strictly newest first, containing the item just
  created; page 2 shared no ids with page 1; a non-member got `404 not_found`; and an item-list
  cursor handed to the feed was `422`.
- *Read-only.* `POST`, `PATCH` and `DELETE` on the timeline were each `405`.
- *Concurrency, over real HTTP.* Two identical comments sent at the same instant returned `200`
  and `201` with the same id, and the database held **exactly one** comment and **exactly one**
  `commented` event for that note.
- *Closure does not stop the discussion.* An item was walked to `closed`; a comment on it was
  `201`, and the item's `version` and `status` were unchanged afterwards.

**A test bug found and fixed rather than worked around.** The first version of the pagination
fixture inserted comments with `uuid.uuid4()` ids and asserted the page order equalled insertion
order. It failed, correctly: UUIDs are random, so `id DESC` is a total order but not the
insertion order. The assertions were rewritten to test the real property — that the walk visits
every row exactly once in strictly descending `(created_at, id)` order — and the fixture gained
an explicit `spaced` mode that gives rows distinct timestamps, which is what makes "newest
first" a deterministic claim instead of a race against the clock. A separate case keeps the
shared-timestamp mode and asserts the timestamps really are equal, so the tie-breaker is proven
rather than assumed.

**A design bug the e2e pass found.** A cursor issued by the item list was accepted by the
activity endpoint and then reached SQL as a `bigint < uuid` comparison — a `500` waiting for the
first client that mixed the two up. Cursors now declare which id kind they carry at each call
site, so the mismatch is a `422` at the boundary. This is also what makes "activity paginates
independently of item lists" enforced rather than merely intended.

**Commit.** `136b491` on `feature/newtonite-operations-platform`. No history was rewritten.

**Remaining gaps.** No frontend, so all of this is reachable only through HTTP. Comment editing
and deletion do not exist by design. The e2e pass added three probe items to the development
database (3 009 items, 1 884 comments, 11 391 events in total); they are ordinary rows and the
seed remains additive and idempotent.

**Known defects.** None known.

## Phase 6 — Frontend vertical slice

**Requirements.** A React + TypeScript client that makes the API usable by a person: sign in,
see a team's work items with server-side filters and paging, open an item, claim it, move it
through the workflow, edit it, comment on it and read its history. Loading, empty, success and
error states must all be represented, conflicts must be explained in the user's language, and
the browser must never be the thing that decides what is allowed.

**Acceptance criteria.** Every screen that loads server data shows all four states; filtering
and paging are requests, not client-side work over a loaded page; claim, approve and close are
server-confirmed and a `409`/`412` is explained with a way to recover; a viewer is offered no
write control; a retried create or comment cannot produce a duplicate; and a `401` from any
request returns the user to sign-in rather than to a broken page.

**What was built.** A new Vite application under `frontend/`, with no runtime dependency beyond
React and TanStack Query.

| Area | Files |
| --- | --- |
| HTTP | `api/client.ts` (the error envelope, the `ETag`, the bearer token), `api/types.ts` |
| Session | `auth/session.ts` (a store, not a context), `auth/auth.ts`, `auth/LoginPage.tsx` |
| Shell | `App.tsx`, `components/Layout.tsx`, `components/states.tsx` |
| Items | `ItemListPage`, `ItemFilters`, `ItemList`, `CreateItemForm`, `ItemDetailPage`, `ItemActions`, `ItemEditForm` |
| Discussion and history | `CommentThread`, `ActivityTimeline` |
| Team feed | `features/activity/TeamActivityPage.tsx` |
| Shared | `lib/format.ts`, `lib/errors.ts`, `lib/permissions.ts`, `lib/router.ts`, `lib/idempotency.ts`, `query/client.ts` |
| Tests | 7 files, `test/fetch.ts` (a recorded fake `fetch`), `test/fixtures.ts`, `test/render.tsx` |

**Design details worth recording.** The full reasoning is in `docs/FRONTEND.md` and decisions
19–22 in `ENGINEERING_DECISIONS.md`; the short version:

- *Server state is the only state.* Filters and cursors are query parameters, so "show me every
  unassigned urgent item" is correct rather than "…that happens to be in the loaded page".
- *No optimistic updates on claim, approve, close or edit.* The answer is shown after the
  server gives it. A rolled-back optimistic claim is a worse experience than a button that
  waits.
- *A write's response is adopted, and the `ETag` is stored with the item.* They are one fact.
  After a save, the next conditional write is conditioned on the version the server just
  produced, without an extra read.
- *Retries are opt-in and only where the server can recognise them.* Create and comment retry,
  because they carry an `Idempotency-Key` that is a mutation **variable**; claim, transition and
  edit do not, because a retry after a success would be a `409` or a `412`.
- *A `401` anywhere ends the session*, handled once in the query cache.
- *The token is in memory only*, so a reload signs the user out. The trade-off is stated in
  `SECURITY.md` rather than hidden behind a comment.
- *The role matrix is mirrored but advisory.* The moves offered come from the server's
  `available_transitions`; the mirror only decides which controls to draw.
- *A hand-written hash router*, because there are three views and one nested route and a router
  library is for the things this application does not have.

**Verification.**

| Check | Command | Result |
| --- | --- | --- |
| Frontend tests | `npm run test` | Passed — **55 passed** in 7 files |
| Type check | `npm run typecheck` | Passed — `tsc --noEmit`, strict, `noUncheckedIndexedAccess` |
| Lint | `npm run lint` | Passed — `eslint .`, no warnings |
| Production build | `npm run build` | Passed — 296.58 kB JS (89.24 kB gzip), 8.89 kB CSS (2.34 kB gzip) |
| Backend tests (unchanged, re-run) | `uv run pytest -q` | Passed — 186 passed |
| API contract over real HTTP | `uv run python` script against the seeded database and a real `uvicorn` process | **41/41 checks passed** — see below |
| CORS preflight | `OPTIONS /api/v1/items` with `Origin: http://localhost:5173` | Passed — `access-control-allow-origin: http://localhost:5173`, allowing `Authorization`, `Content-Type`, `Idempotency-Key`, `If-Match` |
| CI (GitHub Actions) | run [`38980249711`](https://github.com/mahinigam/newtonite-ops-23131011193/actions/runs/38980249711) | Passed — both jobs green, including the new frontend job (`npm ci`, lint, typecheck, test, build) |

**The contract check, and why the component tests are not enough.** The frontend tests stub
`fetch`, which is the right way to test a component but proves nothing about the wire format.
So the same requests the browser makes were sent to the running API — same paths, same repeated
query parameters, same headers — and checked:

- `POST /auth/login` with the browser `Origin` returns `200` and an
  `access-control-allow-origin` naming that origin.
- `GET /teams/{id}/items` with **repeated** `status` parameters returns only rows matching any
  of them, and a repeated `priority` narrows it further; the page envelope is exactly
  `{items, next_cursor, has_more, limit}` and `limit` is echoed.
- `assignee=none` returns only unassigned rows; `assignee=me` is empty before the claim and
  contains the item afterwards.
- `q=refund` is accepted and applied server-side.
- `POST /teams/{id}/items` with an `Idempotency-Key` returns `201` with an `ETag`, and the
  `ETag` is **exposed** through CORS (`access-control-expose-headers`) — without which the
  browser could not read the header and every conditional write would be impossible.
- `PATCH` with `If-Match` returns `200` and bumps the version once; the same `ETag` again
  returns `412 stale_version`.
- `POST /claim` makes the caller the assignee; `POST /transitions` advances the status and
  returns a server-computed `available_transitions`.
- `POST /comments` returns `201`; the thread and the item timeline both contain it, newest
  first, with `created` last; the team feed contains the item.
- `GET /teams/{uuid}/items` for an unknown team returns `404 not_found`.

**A test bug found and fixed rather than worked around.** Four component tests failed on first
run with "found multiple elements". The cause was not the components: `@testing-library/react`
registers its own DOM cleanup through a global `afterEach`, and with `globals: false` it cannot
find one, so every render stayed in `document.body` for the next test. The fix was an explicit
`cleanup()` in the test setup — and the fact that the failure looked like a component bug is
exactly why the tests were worth having.

**A real bug the tests found.** `explainError` had its `validation_failed` branch inside a
lookup that could never match that code, so field-level errors fell through to a generic
sentence. The test that named the offending field failed, and the branch was moved. Without the
test the bug would have shipped as slightly worse wording, which is the kind of defect that is
never reported.

**Commit.** `0316859` on `feature/newtonite-operations-platform`. No history was rewritten.

**Remaining gaps.** No member-management UI (the API supports it, admin only). No offline
support. The e2e contract check added one probe item to the development database, which is an
ordinary row.

**Known defects.** None known.

## Phase 7 — Transactional outbox, worker and notifications

**Requirements.** The phase was conditional on the critical correctness tests and the
frontend vertical slice passing. Both did (Phase 4–6), so the gate opened. The Phase 7
assessment that stood here before concluded that an outbox with **no consumer** would be
infrastructure with nothing to do — and that was right at the time. This phase supplies the
consumer: in-app notifications for the three moments that have an obvious recipient.

**What was built.**

| Piece | Where | Notes |
| --- | --- | --- |
| `outbox_events` | migration `0003` | `BigSerial` id, known event types, `pending`/`delivered`/`failed`, `attempts`, `next_attempt_at`, `last_error`. Partial index on the claim query |
| `notifications` | migration `0003` | One row per event and recipient, with `read_at`. Unique `outbox_event_id` is the exactly-once constraint |
| Producer | `services/outbox.py`, called from `services/work_items.py` | Written in the caller's transaction; it never commits |
| Worker | `app/worker.py`, `services/outbox.py` | Claims with `FOR UPDATE SKIP LOCKED`, delivers each event in its own savepoint, bounded backoff, terminal `failed` |
| Read API | `api/v1/notifications.py` | List (keyset), unread count, mark one, mark all. Person-scoped by the token |
| Operator visibility | `api/v1/ops.py` | Per-team delivery counts, admin-only. Counts only — no titles or recipients |
| Person-scoped work | `api/v1/me.py`, `services/work_items.my_items_statement` | `/me/items`: assigned to the caller, across the caller's memberships |

**Why a separate process, and why no broker.** The state change, its activity event and its
outbox row commit together, so "the assignment happened but nobody will ever be told" is
impossible — the same guarantee history already had, extended to delivery. The worker is a
separate process so a slow notification cannot slow a request and a restart cannot stop
delivery. No broker was added: PostgreSQL has row locking and `SKIP LOCKED`, which is exactly
what a queue needs, and a broker would introduce a second place where the truth lives. Full
reasoning and rejected alternatives: `ENGINEERING_DECISIONS.md` 23.

**At-least-once is stated, not hidden.** A worker can deliver and crash before committing, so
the same event can be processed twice. `notifications.outbox_event_id` is unique and the
consumer inserts with `ON CONFLICT DO NOTHING`, which makes the *effect* exactly-once. The
tests assert the effect rather than claiming exactly-once delivery.

**Deliberate product limits, recorded so they are not read as omissions.** Only assignment,
approval and rejection notify. Claims, comments and ordinary status moves appear in the
activity feed and would make the inbox noise. A recipient who has left the team is not
notified; the event is marked delivered. Marking an already-read notification read again
succeeds without rewriting `read_at`.

**Defects found and fixed while building it.**

| Defect | How it surfaced | Fix |
| --- | --- | --- |
| The claim response's `ETag` was discarded by the client, so claim (which bumps the version) followed by a transition sent `If-Match: "1"` and got `412` | A new frontend test failed on the *next* transition after a successful claim | `claimItem` returns the parsed response including its `ETag`; the test was run against the old client to confirm it failed there |
| Migration `0003` double-prefixed its `CHECK` constraint names (`ck_outbox_events_ck_outbox_events_…`) because the metadata naming convention applies to the suffix it is given | `tests/test_index_inventory.py` compares declared names with `pg_constraint` | The migration now passes the bare suffix, matching the models. Both databases were downgraded to `0002` and re-upgraded; `alembic check` was already green, which is exactly the drift that test exists to catch |
| Two `NotificationsPage` tests queried a composite line with an exact-string matcher, and one regex matched both the body and the meta line | The suite failed with "found multiple elements" / "unable to find" | The assertions now match the line (`/^Platform ·/`); one fixture body was reworded so the test is unambiguous. The component was correct |

**Verification.**

| Check | Command | Result |
| --- | --- | --- |
| Backend suite | `uv run pytest` | **229 passed** in 49.8 s against real PostgreSQL |
| Frontend suite | `npx vitest run` | **79 passed** in nine files |
| Formatting, lint, types | `uv run ruff format --check .`, `uv run ruff check .`, `mypy .`, `npx eslint .`, `npx tsc --noEmit` | Clean (88 backend source files checked by mypy) |
| Migration drift | `uv run alembic check` | "No new upgrade operations detected" |
| Index and constraint inventory | `uv run pytest tests/test_index_inventory.py` | 17 passed — including the two new notification indexes, the partial outbox index, and the new named constraints |

## Phase 8 — Documentation, the container stack, and reviewer readiness

**Requirements.** Re-run every gate on the finished system; make the documentation describe
what runs; give a reviewer a path that does not depend on their toolchain; and check that
none of the earlier claims drifted once the new features landed.

**One command.** `docker compose up --build` now runs PostgreSQL, the API, the outbox worker
and the web app. Migrations and the idempotent seed run in the API's entrypoint, the worker
starts once the API reports ready, and nginx serves the built SPA while forwarding `/api` to
the API. That last choice is deliberate: one origin means no CORS permission in the
containerised setup and no host name baked into the image — `VITE_API_BASE_URL` is built as an
empty string, which the client reads as "same origin". Reasoning and trade-offs:
`ENGINEERING_DECISIONS.md` 27.

**Documentation audit, re-run after Phase 7.** Documentation that drifts is worse than none,
so every load-bearing claim was checked against the running system rather than re-read:

| Claim | How it was checked | Result |
| --- | --- | --- |
| The API surface in `docs/API.md` | Enumerated the app's OpenAPI schema and compared | Every operation documented; the four notification routes, `/me/items` and `/ops/outbox` were added in this phase |
| The demo accounts in `README.md` | Compared against the users `scripts/seed.py` creates | 12/12 match exactly |
| The `Makefile` targets named in the README | `make help` | Every documented target exists, including `worker`, `worker-once`, `stack-up`, `stack-down` |
| `.env.example` | Read it | Placeholders only; the outbox settings were added |
| Test counts stated in the README and here | `uv run pytest --collect-only -q`, `npx vitest run` | 229 backend, 79 frontend — both match |
| Filter values in `docs/API.md` | Compared with `app/models/enums.py` | **Corrected**: the documented item types were `task, bug, incident, payment, request`; the real set is `incident, customer_issue, payment, compliance, task` |
| The index claim for `ix_work_items_assignee_created` | `scripts/explain_lists.py`, extended with the `/me/items` shape | **Corrected and measured**: the team list intersects it with the team index; `/me/items` seeks it alone. Both plans are now in `docs/DATA_MODEL.md` |
| The "not built" list in `docs/KNOWN_LIMITATIONS.md` | Compared against the route table, the worker and the frontend's views | Accurate — the outbox row was replaced by the three genuine gaps (second channel, push, shareable presets) |

**Verification.**

| Check | Command | Result |
| --- | --- | --- |
| Every gate at once | `make check` | Backend lint + format + types + **229 tests**, frontend lint + types + **79 tests**, production build |
| Production build | `npm run build` | Passed — 306.21 kB JS (91.24 kB gzip), 10.56 kB CSS (2.65 kB gzip) |
| Migration drift | `uv run alembic check` | "No new upgrade operations detected" |
| Container stack, built and run | `docker compose up --build` | Four services healthy; API `/readyz` green; worker started and idle |
| Real-HTTP checks through the container's nginx origin | scripted against `http://localhost:8080` | **26/26 passed** — proxied health, login, claim and its `ETag`, assignment, worker delivery, notification list and count, acknowledgement, `/me/items`, cross-team `404`, stale `412`, activity |
| Earlier end-to-end suites | The Phase 5 suite (67) and the Phase 6 contract suite (41) | **108/108 passed** on the merged `main` before this phase's changes |
| Fresh clone from GitHub | `git clone`, `make setup`, `make migrate`, `make seed ITEMS=50` into an empty database, `make check`, then the API and the worker on `:8011` | Migrations `0001`→`0003` and 50 items from scratch; 229 + 79 tests green; **32/32** HTTP checks — idempotent replay, a claim followed by a transition using the claim's `ETag`, an illegal transition, a missing precondition, worker delivery, unread count, acknowledgement, `/me/items`, cursor paging, cross-team `404`, stale `412`, and the `/ops/outbox` role gate |

**What was not weakened to get here.** No test was skipped, deleted or relaxed, and no
assertion was rewritten to match observed behaviour. Two of the three defects above were in
the implementation (the discarded `ETag`, the migration's constraint names) and were fixed
there; the third was in two test queries that did not express what they meant, and the reason
is recorded rather than silently adjusted.

**Repository name.** The execution plan requires `newtonite-ops-<rollno>`, so the repository
was renamed to `mahinigam/newtonite-ops-23131011193` at the end of this phase. GitHub
redirects the old URL, and the clone command in the README was updated and then re-verified
from the new name.

**Known defects.** None known. The residual risks are the documented ones in
[`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) — chiefly no login rate limiting, no
browser-driven end-to-end test in CI, and no hosted deployment.

## How to read this document

- A phase is only "Complete" when every acceptance criterion has been verified with a
  command whose output was observed.
- "Not run" is used rather than implying success. Anything not verified is stated as such.
- Commit SHAs are recorded in the row for each phase once the phase commit is pushed.
