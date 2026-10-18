# Data Model

The database is the authoritative source of truth. This document describes what is
stored, which rules the database itself enforces, why each index exists, and measured
query behaviour. It reflects migrations `0001_initial_schema`, `0002_team_created_index` and
`0003_outbox_and_notifications`.

## Entities

```
users ──< memberships >── teams
  │                         │
  │                         ├──< work_items ──< comments
  │                         │         │
  │                         │         ├──< activity_events   (append-only history)
  │                         │         └──< notifications ──> outbox_events
  │                         └──< outbox_events               (queued in the same transaction)
  └──< auth_sessions
  └──< idempotency_keys
```

| Table | Holds | Notes |
| --- | --- | --- |
| `users` | Accounts and Argon2id password hashes | Unique, lower-cased `email` |
| `teams` | Teams, addressed by a short unique `key` | |
| `memberships` | A user's role **inside one team** | The only place a role exists |
| `work_items` | The operational record | Belongs to exactly one team; carries `version` |
| `comments` | Human notes on an item | Append-only: no edit or delete path |
| `activity_events` | Append-only history | Written in the same transaction as the change |
| `auth_sessions` | Login sessions | Stores only a SHA-256 digest of the token |
| `idempotency_keys` | Retry records for writes | Keyed on `(actor_id, operation, key)` |
| `outbox_events` | Work queued for asynchronous delivery | Written in the mutation's transaction and never committed on its own; drained by `python -m app.worker` |
| `notifications` | One delivered notification per event and recipient | Delivery state, not history: it has `read_at` and is deleted when the recipient leaves the team |

Roles are per team, not global: one account can be a `member` in one team and a `viewer`
in another. The seed data contains exactly that case, and a test asserts it.

## Conventions

| Convention | Choice | Reason |
| --- | --- | --- |
| Primary keys | `uuid` defaulting to `gen_random_uuid()` | Ids can be generated and compared without a round trip, and do not leak record counts |
| History keys | `bigint` identity | Append-only rows want a monotonic key; it gives a strict total order for keyset pagination without depending on timestamp precision |
| Timestamps | `timestamptz`, defaulted by the database | Unambiguous across time zones; the server clock is the only clock |
| Enum-like values | `varchar` + named `CHECK` constraint | Adding a status is an additive migration rather than a PostgreSQL type rewrite |
| Optimistic concurrency | `work_items.version` integer, bumped by the update itself | The version comparison and the bump happen in one statement, so two racing writers cannot both succeed |

## Integrity rules and where they are enforced

Application validation produces good error messages. These rules do **not** depend on it.

| Rule | Mechanism | Test |
| --- | --- | --- |
| An assignee must be a member of the item's team | Composite FK `work_items(team_id, assignee_id) → memberships(team_id, user_id)` | `test_schema_constraints.py::test_assignee_from_another_team_is_rejected_by_the_database` |
| An unassigned item is valid | Same FK under `MATCH SIMPLE`, which skips the check when `assignee_id` is null | `test_unassigned_item_is_allowed` |
| At most one role per user per team | `UNIQUE(team_id, user_id)` | `test_one_role_per_user_per_team` |
| History cannot be rewritten | `BEFORE UPDATE OR DELETE` trigger raising `restrict_violation` | `test_activity_history_is_append_only` |
| Status, priority, type, role are from known sets | Named `CHECK` constraints | `test_status_is_constrained`, `test_index_inventory.py` |
| Title is 3–200 characters, description ≤ 20 000, comment 1–4 000 | `CHECK` constraints | `test_title_length_is_enforced` |
| `version` never drops below 1 | `CHECK (version >= 1)` | inventory test |
| A notification recipient must be in the team it is about | Composite FK `notifications(team_id, recipient_id) → memberships(team_id, user_id)` | `test_a_notification_requires_a_recipient_in_the_team` |
| One outbox event produces at most one notification | `UNIQUE(outbox_event_id)` — the exactly-once half of at-least-once delivery | `test_the_same_outbox_event_cannot_produce_two_notifications` |
| Leaving a team removes the notifications about it | `ON DELETE CASCADE` on that composite FK | `test_leaving_a_team_removes_the_notifications_about_it` |
| Outbox event types and statuses are known; `attempts >= 0` | Named `CHECK` constraints | `test_outbox_status_is_constrained`, `test_outbox_event_type_is_constrained`, inventory test |
| A notification body is 1–500 characters | `CHECK (char_length(body) BETWEEN 1 AND 500)` | `test_a_notification_must_say_something` |
| An idempotency key is unique per actor and operation | `UNIQUE(actor_id, operation, key)` | inventory test; exercised in Phase 4 |
| Email looks like an email | `CHECK (position('@' in email) > 1)` — defence in depth, not validation | inventory test |

`TRUNCATE` is deliberately still allowed on `activity_events` so test fixtures can reset
the table; ordinary `UPDATE` and `DELETE` are refused.

## Indexes, and the query each one serves

| Index | Serves |
| --- | --- |
| `ix_work_items_team_status_created` on `(team_id, status, created_at DESC, id DESC)` | Team-scoped item lists filtered by status, newest first. The workhorse for the list and dashboard views |
| `ix_work_items_team_created` on `(team_id, created_at DESC, id DESC)` | The same list **without** a status filter — the default view of a team's work. Added in revision `0002` after measuring that the index above cannot serve it (its second column is `status`, so there is nothing to seek on after `team_id`) |
| `ix_work_items_assignee_created` on `(assignee_id, created_at DESC, id DESC)` | Both "assigned to me" shapes. The team list's `assignee=me` is this index intersected with the team index; `/me/items` seeks this index and restricts the result to the caller's membership set. One index, two scopes |
| `ix_work_items_unassigned_urgent` on `(team_id, created_at DESC, id DESC) WHERE assignee_id IS NULL AND status = 'open' AND priority IN ('high','urgent')` | The unassigned-urgent queue. Partial so it stays small and only covers rows that queue can return |
| `ix_work_items_search_vector` (GIN) | Full-text search over title + description |
| `ix_activity_events_work_item_id_created_at` on `(work_item_id, created_at DESC, id DESC)` | One item's timeline, newest first — the item activity endpoint |
| `ix_activity_events_team_id_created_at` on `(team_id, created_at DESC, id DESC)` | A team's recent activity feed, newest first. The event's denormalised `team_id` is the authorization and filter key; the feed joins `work_items` only to read the current item title |
| `ix_comments_work_item_id_created_at` on `(work_item_id, created_at, id)` | One item's comment thread. Declared ascending because that is the natural way to write it; the API reads it newest-first, which is the same index read backwards (or, for the handful of rows an item actually has, a bitmap scan of those rows and a tiny sort) |
| `ix_memberships_user_id` on `(user_id)` | "Which teams does this user belong to" — the prefix the composite unique constraint cannot answer |
| `ix_auth_sessions_user_id` on `(user_id)` | "Revoke every session for this user" and per-user session auditing. Token lookup itself uses the unique index on `token_hash` |
| `ix_outbox_events_pending` on `(next_attempt_at, id) WHERE status = 'pending'` | The worker's claim query, exactly: pending rows, oldest due first. Partial, so the index tracks the backlog rather than everything ever delivered |
| `ix_notifications_recipient_created` on `(recipient_id, created_at DESC, id DESC)` | A person's notification list, newest first, with `id` as the tie-breaker that makes the keyset cursor total |
| `ix_notifications_recipient_unread` on `(recipient_id, created_at DESC, id DESC) WHERE read_at IS NULL` | The unread badge. Partial, because the count only ever asks about rows that can be unread |

Every list index ends in `id` (descending, except the comment thread's ascending
declaration) so the ordering is a total order: two items created in the same millisecond
still have exactly one correct position. That is what makes keyset pagination stable — and
it is not a theoretical concern, because a row inserted inside a transaction shares that
transaction's `now()`, which is how the pagination tests are built.

### When the partial unassigned-urgent index applies

The two queue rows above are the same feature asked two different ways, and they are worth
reading together.

`ix_work_items_unassigned_urgent` is partial, covering only rows where
`assignee_id IS NULL AND status = 'open' AND priority IN ('high','urgent')`. A partial
index can only serve a query that proves the partial predicate holds, so it is used when
the request filters on all three — the "unassigned urgent open work" queue — and it is
about 2.5× faster there (0.31 ms against 0.82 ms) with a much smaller index to read.

When the same queue is asked **without** constraining `status` (all unassigned urgent items,
whatever their state), the planner cannot use it and falls back to
`ix_work_items_team_created`, which is correct: the broader question genuinely has to
consider more rows. Both plans are recorded rather than only the flattering one.

The index leads with `team_id` because the queue is always scoped to one team, so a leading
`team_id` lets the index filter and order together. A cross-team version of the query with
no `team_id` predicate can still filter with it but has to sort.

## Measured behaviour

Data: the seed's default dataset of **3 000 work items**, **11 356 activity events**,
**1 878 comments**, 12 users and 14 memberships. Timings are a single local run on the
development machine — they are evidence that the indexes are used and the plans are
bounded, not a performance claim. They vary by roughly ±50% between runs, and more on the
first run after a restart, so the **plan shape** is the durable part of each row and the
milliseconds are not.

Reproduce with:

```bash
uv run python -m scripts.explain_lists
```

That script imports the statement builders from the service layer — `list_statement`,
`comment_list_statement`, `item_activity_statement`, `team_activity_statement` — so the
SQL it explains is the SQL the API executes. Filters, ordering and the cursor all go
through the same builders; a hand-written lookalike would drift the first time a filter
was added.

| Query, as the API issues it | Plan | Time |
| --- | --- | --- |
| Team list, no filter (the default team view) | `Index Scan using ix_work_items_team_created` | **0.23 ms** |
| Team list, filtered by status | `Index Scan using ix_work_items_team_status_created` | **0.15 ms** |
| Team list, filtered by status **and** priority | `Bitmap Index Scan on ix_work_items_team_status_created`, top-N sort | **0.67 ms** |
| Queue: unassigned high-or-urgent items, any status | `Bitmap Index Scan on ix_work_items_team_created` | **0.62 ms** |
| Queue: unassigned high-or-urgent **open** items | `Bitmap Index Scan on ix_work_items_unassigned_urgent` (the partial index), top-N sort | **0.17 ms** |
| Team list, `assignee=me` | `BitmapAnd` of `ix_work_items_assignee_created` and `ix_work_items_team_created` | **0.28 ms** |
| My work (`/me/items`), caller in two teams | `Bitmap Index Scan on ix_work_items_assignee_created`, the membership set applied as a filter, top-N sort | **0.43 ms** |
| Full-text search | `Bitmap Index Scan on ix_work_items_search_vector` | **0.10 ms** |
| Team list, deep page by cursor (300 rows skipped) | `Index Scan using ix_work_items_team_created`, 301 rows removed by filter | **0.30 ms** |
| One item's comment thread, newest first | `Bitmap Index Scan on ix_comments_work_item_id_created_at` | **0.04 ms** |
| One item's timeline, newest first | `Bitmap Index Scan on ix_activity_events_work_item_id_created_at` | **0.05 ms** |
| Team activity feed, first page | `Index Scan using ix_activity_events_team_id_created_at` | **0.05 ms** |
| Team activity feed, deep page by cursor (half the feed skipped) | `Index Scan using ix_activity_events_team_id_created_at`, 1 426 rows removed by filter | **0.82 ms** |

Every list query also resolves the assignee, creator or actor through `Memoize`d
primary-key lookups on `users`, so one page costs one lookup per distinct person, not one
per row. The plan for the first item page completes with 44 buffer hits and no sort node.

The two `/me/items` and team-list rows are both measured, because they are the same
question asked at two scopes and the planner answers them differently. A single team lets it
intersect the assignee index with that team's index (`BitmapAnd`); several teams make the
intersection more expensive than filtering, so it seeks the assignee index alone and rejects
the handful of rows outside the caller's memberships. Neither shape sorts the table — the
sort is a top-N over the rows the index produced, and the ordering inside each index is the
same `(created_at DESC, id DESC)` the cursor walks.

The notification indexes are **not** in this table: the seed produces no notifications,
because notifications are delivery state written by the worker rather than demo data. Their
shape is the same keyset shape measured above, and their existence is asserted by
`tests/test_index_inventory.py` rather than by a timing that would be measuring an empty
table.

The two item-scoped rows above are the same shape as each other and are deliberately
reported as they are: the seed's longest thread has three comments and its longest
timeline nine events, so the planner chooses a bitmap scan of exactly those rows and sorts
a handful, which is cheaper than walking an index backwards for so few rows. The index is
still what bounds the work — the alternative would be reading the whole table. The team
feed is where the deep-page measurement is meaningful, because it is the list with
thousands of rows behind it.

### An index added because a measurement asked for it

The first version of this table had no unfiltered team row, and that was the bug: the
default team view was a **sequential scan of the whole table plus a top-N sort** at
3.19 ms, against 0.32 ms for the same list narrowed by status. The cause was structural
rather than a matter of size — `ix_work_items_team_status_created` leads with `team_id`
but its next column is `status`, so a query that does not constrain `status` can seek to
the team and then has nothing to walk in order. It would have stayed a full scan at any
size.

`ix_work_items_team_created` fixes it, and is also exactly the ordering the keyset cursor
walks. Measured before and after adding it, in one sitting on the same data: **3.19 ms →
0.53 ms**, with the sequential scan gone. (The absolute figure drifts between runs as the
table above explains; the before/after comparison is what this paragraph is evidence for,
and it is a same-session comparison of the same query.)

### What keyset pagination does and does not buy

The cursor is a position in `(created_at DESC, id DESC)` rather than an offset, so an item
created while somebody is reading page 1 cannot shift a later page — page 2 continues from
where page 1 stopped. That property is asserted in `tests/test_items_list.py` by creating
rows between two page requests.

The honest limit: the cursor predicate is written in its expanded `OR`/`AND` form and the
plan applies it as a **filter** after seeking to the team in the index, so a deeper page
walks past the rows it is skipping (301 of them for page 13, all inside the index, no heap
fetches). Cost therefore grows with page depth rather than being constant. It remains
strictly better than `OFFSET`, which would additionally read and discard the page's heap
tuples, and at the seeded size the whole query is under a millisecond.

### Full-text search, measured properly

Searching is the case most likely to be misreported, because the plan depends on how the
statement is prepared. All four of these are measured on the same dataset for the term
`timeout`, which matches 140 items:

| How the statement is issued | Text-search config | Index condition | Time |
| --- | --- | --- | --- |
| Values inlined (`EXPLAIN` on the compiled statement) | literal | `search_vector @@ '''timeout'''::tsquery` | **0.16 ms** |
| Prepared statement, custom plan (values known) | literal | `search_vector @@ websearch_to_tsquery('english'::regconfig, $2)` | **0.17 ms** |
| Prepared statement, **forced generic plan** | literal | `search_vector @@ websearch_to_tsquery('english'::regconfig, $2)` | **0.245 ms** |
| Prepared statement, **forced generic plan** | **parameter** | `search_vector @@ websearch_to_tsquery($2, $3)` | **5.47 ms** |

The last row is the one worth reading. It still shows a `Bitmap Index Scan`, which would
look like success in a screenshot, but the index condition is the function call itself:
with the configuration passed as a parameter, PostgreSQL cannot evaluate the tsquery once
and instead re-evaluates `websearch_to_tsquery` for **every index entry** it considers.
Same plan *shape*, about 20× the work.

That is why `SEARCH_CONFIG` in `app/services/work_items.py` is written as the literal
`'english'::regconfig` rather than a bound value. Using a literal for a fixed internal
constant is what allows the tsquery to be evaluated once. Nothing user-supplied is
interpolated — the search term itself is always a bound parameter.

Unlike the earlier revision of this document, the GIN index **is** used by the planner at
3 000 rows. The previous measurement reported a sequential scan; that was measured against
a different query shape, and the claim was corrected to match the plan rather than kept for
consistency. No claim is made about behaviour at the brief's tens-of-thousands-of-items
target, which has not been measured.

## What the schema deliberately does not do

- No soft-delete column: items are resolved or closed, not deleted, and there is no delete
  endpoint. History therefore never dangles.
- No `updated_by` on `work_items`: who changed what is in `activity_events`, which keeps a
  full record instead of only the last editor.
- No cascade from `work_items` to `activity_events`: history is deleted only by deleting
  the whole table, which the application never does.
