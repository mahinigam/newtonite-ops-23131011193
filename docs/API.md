# API Reference

Base path: `/api/v1`. The generated OpenAPI schema is served at `/api/v1/openapi.json` and a
browsable UI at `/docs`.

**This document lists what is implemented.** Work items, their filters, pagination, claiming,
the approval workflow, comments and the activity read API are implemented. The frontend and
the outbox worker are not yet; they are tracked in
[`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

## Authentication

State-changing and reading endpoints require a bearer token obtained from `login`.

```
Authorization: Bearer <token>
```

The token is 43 characters of base64url (256 bits of `secrets` randomness). The server stores
only its SHA-256 digest, so **the token is shown exactly once**, in the login response, and
cannot be recovered. A client that loses it must sign in again.

A session ends when any of the following is true:

| Condition | Result |
| --- | --- |
| No `Authorization` header, or a scheme other than `Bearer`, or an empty token | `401 not_authenticated` |
| Digest unknown, or the session was revoked by logout | `401 invalid_session` |
| Past the absolute lifetime (12 h from login) | `401 session_expired` |
| Idle for longer than the idle window (2 h) | `401 session_expired` |
| The account was deactivated | `401 invalid_session` |

`401` responses carry `WWW-Authenticate: Bearer`. Lifetimes are configuration
(`SESSION_ABSOLUTE_TTL_HOURS`, `SESSION_IDLE_TTL_HOURS`). Both expiries are evaluated with
the database clock. Every authenticated request refreshes the idle window, but the refresh is
written at most once a minute per session, so the idle window can be enforced up to about a
minute late.

### `POST /auth/login`

```json
{ "email": "sam.multi@newtonite.dev", "password": "Newtonite!2026" }
```

`200`:

```json
{
  "token": "…43 characters…",
  "token_type": "Bearer",
  "expires_at": "2026-10-18T09:13:13.688524Z",
  "user": { "id": "…", "email": "sam.multi@newtonite.dev", "display_name": "Sam Alvarez" }
}
```

An unknown email, a wrong password and a deactivated account all return **the same** `401
invalid_credentials` body. The endpoint cannot be used to discover which addresses have
accounts. There is no rate limiting yet — see
[`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md).

### `POST /auth/logout`

Revokes the session the request is using. Immediate for every later request with the same
token. Returns `{"revoked": true}`. Because revocation takes effect at once, calling logout
twice returns `401` the second time.

### `GET /auth/me`

The caller and their role in each team they belong to.

```json
{
  "user": { "id": "…", "email": "sam.multi@newtonite.dev", "display_name": "Sam Alvarez" },
  "memberships": [
    { "team_id": "…", "team_key": "finance",  "team_name": "Finance Operations",  "role": "viewer" },
    { "team_id": "…", "team_key": "platform", "team_name": "Platform Engineering", "role": "member" }
  ]
}
```

This is what the frontend uses to decide what to offer. It is **advisory to the interface
only**: every write is authorized again on the server from stored data.

## Teams

Team fields are prefixed (`team_id`, `team_key`) so a nested team can never be mistaken for the
enclosing resource, and the shape is identical here and inside a membership.

### `GET /teams`

Teams the caller belongs to, with the caller's role. Only ever returns their own teams.

### `GET /teams/{team_id}/members`

The roster. Visible to **any** member of the team, including a `viewer`. Sorted by role, then
name.

```json
[{ "user_id": "…", "email": "…", "display_name": "…", "role": "lead" }]
```

### `PUT /teams/{team_id}/members/{user_id}` — admin only

```json
{ "role": "lead" }
```

Grants or changes a role, creating the membership if the person is not in the team yet.

Refusals:

| Situation | Response |
| --- | --- |
| Caller is not a member of this team | `404 not_found` (indistinguishable from a nonexistent team) |
| Caller is in the team but is not an admin | `403 forbidden` |
| The target shares no team with the caller, or does not exist | `404 not_found` |
| `role` is not one of `viewer`, `member`, `lead`, `admin` | `422 validation_failed` |
| Any field other than `role` is present | `422 validation_failed` |
| The change would leave the team with no admin | `409 last_admin` |

### `DELETE /teams/{team_id}/members/{user_id}` — admin only

`204` with an empty body on success.

| Situation | Response |
| --- | --- |
| Not a member, or not an admin | `404`, `403` as above |
| Last admin of the team | `409 last_admin` |
| Still the assignee of work items in this team | `409 member_has_assigned_items` — reassign first. Removing a member never silently unassigns their work, and never touches the history they created |

## Work items

Work items belong to a team. Creating and listing are team-scoped
(`/teams/{team_id}/items`), so the team boundary is part of the URL; reading and changing a
single item address it directly (`/items/{item_id}`) and the item's **own** team is then
authorized against the caller's membership. A client-supplied team or role is never
consulted.

Three headers carry the correctness guarantees:

| Header | On | Purpose |
| --- | --- | --- |
| `Idempotency-Key` | create | **Required.** Makes a retry safe |
| `If-Match` | update, transitions | **Required.** The version the caller believes it is editing |
| `ETag` | every item response | The version to send back as `If-Match` |

### Item shape

A list returns a **summary**; a single-item read adds `description` and `created_by`. The
list does not carry a 20 000-character description per row.

```json
{
  "id": "…",
  "team_id": "…",
  "title": "Refund the duplicate charge",
  "type": "payment",
  "status": "in_progress",
  "priority": "urgent",
  "assignee": { "id": "…", "email": "…", "display_name": "…" },
  "due_at": null,
  "version": 3,
  "created_at": "…",
  "updated_at": "…",
  "available_transitions": ["awaiting_approval"]
}
```

`available_transitions` is **computed on the server** for this caller's role, so the buttons
an interface offers and the transitions the server would accept are the same list. It is
still advisory — every transition is validated again when attempted.

### `POST /teams/{team_id}/items` — requires `Idempotency-Key`

```json
{
  "title": "Refund the duplicate charge",
  "description": "Customer was charged twice.",
  "type": "payment",
  "priority": "urgent",
  "assignee_id": null,
  "due_at": null
}
```

`title`, `type` are required; `priority` defaults to `medium`. There is no `team_id`,
`status`, `version` or `created_by` field — the team comes from the path, the status always
starts at `open`, and the rest is the server's. Sending them is a `422`, not a silently
ignored field.

| Situation | Response |
| --- | --- |
| First delivery of the key | `201 Created`, `ETag` |
| The **same** key and the same body again | `200 OK`, same item, `Idempotent-Replay: true` |
| The same key with a **different** body | `409 idempotency_key_reuse` |
| No `Idempotency-Key` header, or an empty one | `400 idempotency_key_required` |
| Key longer than 255 characters | `422 validation_failed` |
| Assigning to somebody else, without the permission | `403 forbidden` |
| Assigning to somebody not in this team | `422 invalid_assignee` |

The key is scoped to `(caller, operation, key)`, so two people using the same key do not
collide, and a reused key returns **that caller's** original item. See
[`../ENGINEERING_DECISIONS.md`](../ENGINEERING_DECISIONS.md) decision 10.

### `GET /teams/{team_id}/items` — filtering, search and paging

| Parameter | Meaning |
| --- | --- |
| `status` | `open`, `in_progress`, `awaiting_approval`, `resolved`, `closed`. **Repeatable** |
| `priority` | `low`, `medium`, `high`, `urgent`. Repeatable |
| `type` | `incident`, `customer_issue`, `payment`, `compliance`, `task`. Repeatable |
| `assignee` | `me`, `none`, or a user id |
| `q` | Full-text search over title and description (max 200 characters) |
| `limit` | 1–100, default 25 |
| `cursor` | `next_cursor` from the previous page |

Filters combine with `AND`; repeated values within one filter combine with `OR`
(`?priority=high&priority=urgent`). Every filter is applied in PostgreSQL — the browser is
never sent a dataset to filter itself.

```json
{ "items": [ … ], "next_cursor": "eyJjcmVhdGVkX2F0Ijoi…", "has_more": true, "limit": 25 }
```

Ordering is `created_at DESC`, with `id DESC` as a tie-breaker, so the order is **total** —
two items created in the same millisecond still have exactly one correct position. That is
what makes paging stable.

**Paging is keyset, not `OFFSET`.** Pass `next_cursor` back to continue. An item created
while somebody is reading page 1 therefore cannot shift a later page: page 2 resumes from
the position page 1 stopped at, rather than from a row count. The cursor is opaque, unsigned
and carries no permission decision — authorization is re-derived from the row on every
request, so a tampered cursor cannot reach another team's data. A malformed cursor is
`422`, not a silent restart from the beginning.

| Situation | Response |
| --- | --- |
| `limit` outside 1–100 | `422 validation_failed` |
| Malformed `cursor` | `422 validation_failed` |
| `assignee` neither `me`, `none` nor a UUID | `422 validation_failed` |
| Not a member of the team | `404 not_found` |

### `GET /me/items` — everything assigned to the caller

The same filters and the same keyset cursor, without a team in the path. The scope is the
caller's own assignments across **every** team they belong to, derived from the token and
from stored memberships rather than from anything the client sends.

| Parameter | Meaning |
| --- | --- |
| `status`, `priority`, `type`, `q` | As above |
| `limit`, `cursor` | As above |

There is deliberately **no `assignee` parameter**. It would turn a personal view into a
general cross-team list and widen what one token can read; the answer to "who is this for"
is the token, not a query string. An unknown query parameter is ignored rather than consulted,
so sending `assignee` cannot widen the result either — it simply has nothing to do here. An
item appears only while the caller is still a member of its team, so leaving a team removes
its work from the view immediately.

```json
{ "items": [ … ], "next_cursor": null, "has_more": false, "limit": 25 }
```

This is what the **My work** view calls. The team list remains the right endpoint for "what
is happening in this team"; this one answers "what is on my plate".

### `GET /items/{item_id}`

Returns the detail shape and an `ETag` header, for example `ETag: "3"`. An item in a team
the caller does not belong to is `404`, byte-identical to an id that was never issued.

### `PATCH /items/{item_id}` — requires `If-Match`

```json
{ "title": "…", "priority": "urgent" }
```

Partial: only the fields actually present are applied, so an omitted field means *no
opinion*. Sending a field as `null` **clears** it, which is how an item is unassigned or a
due date removed. `status` is not editable here — status changes go through `transitions`
below, so a generic edit cannot bypass approval or closure.

| Situation | Response |
| --- | --- |
| No `If-Match` header | `428 precondition_required` |
| `If-Match` does not match the current version | `412 stale_version`, body carries `details.current` |
| `If-Match` not a version number | `422 validation_failed` |
| Cannot change this field with this role | `403 forbidden` |
| Item is in another team | `404 not_found` |
| `status`, `version`, `team_id` or `created_by` in the body | `422 validation_failed` |

A request that changes nothing succeeds, and does **not** bump the version or write history —
a form that submits unchanged values is not an event.

**On assignment.** Moving work to or from *somebody else* (or clearing another person's
assignment) requires the `assign others` permission — that is a lead action. Taking an
unassigned item, or releasing your own, is a `claim`/`unclaim` and a member may do it. So a
member reassigning a colleague's item gets `403`, while the same member claiming an
unassigned one gets `200`.

### `POST /items/{item_id}/claim`

Takes ownership of an unassigned, open item. **No `If-Match`** is expected, and none is
needed: the condition that decides a race is `assignee_id IS NULL AND status = 'open'`,
evaluated inside the `UPDATE` itself. A version check would be the *weaker* guard here — it
can pass while somebody else is claiming the same item.

| Situation | Response |
| --- | --- |
| Free and open | `200`, item assigned to the caller, version bumped, one `claimed` event |
| Already owned by the caller | `200`, unchanged — a double-click is a no-op, not a second event |
| Owned by somebody else | `409 item_already_claimed` |
| Not open (for example already started) | `409 invalid_transition` |
| Viewer, or not in the team | `403 forbidden` / `404 not_found` |

Claiming sets the **assignee only**. It does not start the work; that is a separate,
explicitly recorded transition.

### `POST /items/{item_id}/transitions` — requires `If-Match`

```json
{ "to": "awaiting_approval", "note": "Ready for review." }
```

`note` (max 2000 characters) is optional and is recorded in the history — it is how a
rejection carries its reason.

**There is no endpoint that writes `status` directly.** Legality, permission and the version
precondition are all checked before anything changes.

| From | To | Permission | Recorded as |
| --- | --- | --- | --- |
| `open` | `in_progress` | `transition work` | `status_changed` — **requires an assignee** |
| `in_progress` | `awaiting_approval` | `transition work` | `status_changed` |
| `awaiting_approval` | `resolved` | `approve` (lead) | `approved` |
| `awaiting_approval` | `in_progress` | `approve` (lead) | `rejected`, with the note |
| `resolved` | `closed` | `close` (lead) | `status_changed` |
| `closed` | *any* | — | Refused: closing is terminal |

| Situation | Response |
| --- | --- |
| Not an edge in the table above | `409 invalid_transition`, body carries `details.available_transitions` |
| `open → in_progress` with no assignee | `409 assignee_required` |
| The role does not hold the required permission | `403 forbidden` |
| No `If-Match` / stale `If-Match` | `428 precondition_required` / `412 stale_version` |
| `to` is not a known status | `422 validation_failed` |

Because the offered transitions and the refusal both come from the same server-side rule
table, an interface can correct itself from the error rather than guess.

**Why advancing a colleague's work is allowed.** The role matrix grants every member
`transition work` for their team, so a member may move an item somebody else owns — that is
what lets a small team cover for one another. Changing *ownership* is a different permission
and remains restricted. The behaviour is asserted in `tests/test_transitions.py` so it is a
recorded decision rather than an accident.

### Optimistic concurrency, in one paragraph

Every item response carries an `ETag` of its `version`. A caller that wants to change the
item sends that back as `If-Match`. The comparison and the `version = version + 1` happen in
the **same `UPDATE`**, so two clients holding the same version cannot both succeed: the
first wins, the second is told `412 stale_version` and given the current representation in
`details.current` so it can refresh and retry. The precondition is deliberately
**required** rather than optional — an API that silently applies last-write-wins is exactly
the failure mode the brief asks to avoid.

### History

Every successful mutation writes an `activity_events` row **in the same transaction** as the
state change, so there is no state change without an explanation and no explanation without
a state change. If the history write fails, the mutation rolls back — proven by
`tests/test_activity_atomicity.py`, which breaks the history insert with a trigger and
asserts the item is byte-unchanged afterwards.

Fields changed by one request produce one event each (in a fixed field order) while bumping
the version **once**. History is readable through the two endpoints below.


### `GET /users/directory?query=`

People the caller may need to assign work to: only those who share at least one team with
the caller, capped at 50, filtered in the database by `query` (a name or email fragment,
max 100 characters). A caller cannot use it to enumerate accounts outside their own teams.

```json
[{ "id": "…", "email": "…", "display_name": "…" }]
```

## Comments

Comments are addressed through the item they belong to. A comment has no meaning outside its
item, and the item's own row is what the team is derived from — the caller is authorized
against that, never against a team id the client sent.

Reading requires `VIEW_TEAM`; writing requires `COMMENT` (member and above). The author and
the item come from the session and the path, so neither can be claimed by a client.

### `POST /items/{item_id}/comments` — requires `Idempotency-Key`

```json
{ "body": "Root cause was the 22:00 deploy." }
```

`201 Created` for the request that created the comment, `200 OK` with
`Idempotent-Replay: true` for a replay of the same request. The same key with a **different
body** is `409 idempotency_key_reuse` rather than a silent replay of the first comment — the
key identifies one logical operation, not a slot to overwrite.

| Refusal | Status | Code |
| --- | --- | --- |
| No `Idempotency-Key` header | `400` | `idempotency_key_required` |
| Empty after stripping, or over 4 000 characters | `422` | `validation_failed` (`details[0].field == "body"`) |
| Viewer | `403` | `forbidden` |
| Item in a team the caller is not in, or an id that was never issued | `404` | `not_found` |

The body is stripped **before** the length is checked, so `"   "` is a `422` naming the
field rather than a database error later. The stored body is the stripped value.

Commenting on a **closed** item is allowed and deliberate: closure ends the workflow, not the
discussion. A comment changes no field of the item, so it does not bump `version`.

### `GET /items/{item_id}/comments`

Newest first, paged with its own cursor. Each comment is:

```json
{
  "id": "…",
  "work_item_id": "…",
  "body": "Root cause was the 22:00 deploy.",
  "author": { "id": "…", "email": "…", "display_name": "…" },
  "created_at": "2026-10-17T09:14:02.118000Z"
}
```

Comments are append-only: there is no edit or delete route. A correction is another comment,
which keeps the discussion honest about what was said when.

## Activity

Two read-only views of the same append-only table. History is produced by the operations it
describes — there is **no route that writes an event**, and the database trigger behind
`activity_events` refuses `UPDATE` and `DELETE` outright. Both views require `VIEW_TEAM`, so
a viewer may read them; a non-member gets `404`, not `403`.

Each event is:

```json
{
  "id": 9041,
  "work_item_id": "…",
  "event_type": "status_changed",
  "from_value": "awaiting_approval",
  "to_value": "resolved",
  "meta": {},
  "actor": { "id": "…", "email": "…", "display_name": "…" },
  "created_at": "2026-10-17T09:14:02.118000Z",
  "work_item_title": "Refund the duplicate charge"
}
```

`from_value` and `to_value` hold the **stored** values and are the fact of the event — a
status, a priority, a type, a title, or a user id for an assignment. `meta` is rendering
convenience only (a display name, a rejection note, a comment id and a bounded preview), and
a client must work without it. `event_type` is one of `created`, `claimed`, `assigned`,
`unassigned`, `status_changed`, `approved`, `rejected`, `priority_changed`,
`due_date_changed`, `type_changed`, `title_changed`, `description_changed`, `commented`.

`work_item_title` is the item's **current** title. It is present on the team feed, where a
row would otherwise say only "this item", and `null` on an item's own timeline, which already
knows which item it is showing. It is joined at read time rather than copied into `meta` at
write time, so a rename shows through the history — unlike a notification body, which is a
message that was sent and is therefore a snapshot.

### `GET /items/{item_id}/activity`

One item's timeline, newest first.

### `GET /teams/{team_id}/activity`

Everything that has happened in one team, newest first. It is built from the event's
denormalised `team_id`, so the feed cannot be assembled by a query that forgot the team
filter. Each row carries the item's current title, because a feed spanning many items is
unreadable without it.

| Parameter | Meaning |
| --- | --- |
| `cursor` | `next_cursor` from the previous page of **this** feed |
| `limit` | 1–100, default 25 |
| `since` | Inclusive lower bound, ISO 8601 **with an offset** |
| `until` | Inclusive upper bound, ISO 8601 **with an offset** |
| `actor` | `me`, or a user id: only that person's actions |

The bounds are inclusive on both ends, which is what a "From / To" control means: a row
written exactly at the boundary belongs to the window. A bound without an offset is
`422 validation_failed` — the instant is otherwise ambiguous — as is `since` later than
`until`, an unparseable timestamp, or an `actor` that is neither `me` nor an id. The cursor
applies on top of the bounds, so paging a range walks that range, and a client that changes
the range starts again from a null cursor.

`actor` can only remove rows from a set that is already scoped to the team. A valid id for
somebody outside the team therefore returns an empty page, not an error, and never another
team's events.

### Paging, and why the cursors are not interchangeable

Both views are bounded (`limit` 1–100, default 25) and paged by keyset with an opaque
`next_cursor`. A cursor belongs to **one list**: it names a position in a specific ordered
result set, and the item lists, comment threads and activity timelines each have their own.
Activity rows are keyed by a bigint identity while the other lists are keyed by a UUID, so a
cursor issued for one list is rejected by another with `422 validation_failed` rather than
being silently reused as a meaningless position. `has_more` answers "is there another page?"
without a second request, and `limit` is echoed so a client can tell a short page from a
clamped one.

## Notifications

Notifications are delivered by the outbox worker **after** the mutation that caused them.
They are addressed to a person, so there is no team in the path; the team boundary is
enforced by the composite foreign key on the row and by the read query.

| Method and path | Purpose |
| --- | --- |
| `GET /notifications` | The caller's notifications, newest first, keyset-paged (`limit`, `cursor`) |
| `GET /notifications/unread-count` | `{"unread": 3}` — one indexed count, for the badge |
| `POST /notifications/{notification_id}/read` | Mark one read. `204`. Idempotent |
| `POST /notifications/read-all` | `{"marked": 2}` — how many rows actually changed |

```json
{
  "id": "0192f0c4-6b1a-7c3d-9e4f-8a7b6c5d4e3f",
  "team_id": "37c42433-0bf7-5c3f-9716-69bfa9124f95",
  "work_item_id": "6f1d0b7e-2a4c-4d55-9a0e-1b2c3d4e5f60",
  "event_type": "work_item_assigned",
  "body": "Lena Fischer assigned you “Refund the duplicate charge”.",
  "created_at": "2026-10-18T09:15:02Z",
  "read_at": null
}
```

* `event_type` is one of `work_item_assigned`, `work_item_approved`,
  `work_item_rejected`. Those are the three moments with an obvious recipient; claims,
  comments and ordinary status moves are covered by the activity feed instead.
* Reading somebody else's notification is `404`, not `403`: an id that is not yours is
  indistinguishable from one that was never issued.
* `read_at` records when the row was **first** read. Marking an already-read notification
  read again succeeds and does not rewrite the timestamp, so a retry cannot change history.
* Leaving a team deletes the notifications addressed to you about it (the composite foreign
  key cascades). Notifications are delivery state, not history; history lives in
  `activity_events`, which is append-only.
* `team_id` is stored on the row so the client can link back to the item without a second
  lookup. If the caller's session no longer includes that team, the row is shown without a
  link rather than with a broken one.

Notifications are **not** part of any item response. Reading a team's work never implies
reading the delivery records about it, and each list has its own cursor.

### `GET /ops/outbox` — team admin only

Delivery counts for the teams the caller administers. It exists because asynchronous
delivery introduces a failure mode a synchronous version would not have: an event can be
queued and never delivered. Making it visible is what keeps that acceptable.

```json
{
  "teams": [
    { "team_id": "…", "team_key": "platform", "pending": 0, "delivered": 12, "failed": 0 }
  ],
  "pending": 0,
  "delivered": 12,
  "failed": 0
}
```

`failed` is the number that matters: it is the only terminal state, so a non-zero value means
an event exhausted its attempts and nothing else will happen to it. Every other row will be
retried. A caller who is not an admin of any team gets `403`; the response carries counts
only — no titles, recipients or payloads.

## Roles

Roles are **per team**. The same account can be a `member` in one team and a `viewer` in
another; `sam.multi` in the demo data is exactly that case.

| Role | Read | Create · comment · claim · edit · move work | Assign others · approve · close | Manage members |
| --- | --- | --- | --- | --- |
| `viewer` | ✓ | | | |
| `member` | ✓ | ✓ | | |
| `lead` | ✓ | ✓ | ✓ | |
| `admin` | ✓ | ✓ | ✓ | ✓ |

The matrix is defined once, in `backend/app/policy/roles.py`, and tested in
`backend/tests/test_authorization.py`.

## Errors

Every failure, including framework-generated ones such as an unknown route, has the same
shape:

```json
{ "error": { "code": "forbidden", "message": "Your role in this team does not allow 'manage_members'." } }
```

`code` is stable and is what a client should branch on. `message` is written to be shown to a
user and never contains a stack trace, SQL, a table name or a constraint name. `details` is
present only for validation failures and lists the offending fields.

| Status | Codes | Meaning |
| --- | --- | --- |
| `400` | `idempotency_key_required` | A create arrived without a usable `Idempotency-Key` |
| `401` | `not_authenticated`, `invalid_session`, `session_expired`, `invalid_credentials` | No usable session — sign in again |
| `403` | `forbidden` | You are in this team, but your role does not permit it |
| `404` | `not_found` | Does not exist, **or** belongs to a team you are not in |
| `409` | `invalid_transition`, `item_already_claimed`, `idempotency_key_reuse`, `idempotency_key_in_progress`, `assignee_required`, `last_admin`, `member_has_assigned_items` | Conflicts with current state |
| `412` | `stale_version` | Your `If-Match` version is out of date; `details.current` carries the current item |
| `422` | `validation_failed`, `invalid_assignee` | The request body or a parameter failed validation |
| `428` | `precondition_required` | A conditional request arrived without `If-Match` |
| `500` | `internal_error` | Unexpected. The detail is logged, never returned |

**Why `404` rather than `403` across a team boundary.** `403` would confirm that the resource
exists. A caller who is not in a team is answered exactly as if they had invented the id, and
a test asserts the two responses are byte-identical. `403` is reserved for a caller who is
already inside the team, where the team's existence is not a secret and the message can
actually help.

## Health

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. Does not touch the database |
| `GET /readyz` | Readiness. Performs a database round trip; `503` when the database is unreachable |
