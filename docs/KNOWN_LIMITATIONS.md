# Known Limitations

This document states what the project does **not** do, why, and what it would take to
address each item. It is updated every phase and reflects the code as pushed.

Last updated: 2026-10-19 (final compliance and documentation accuracy pass).

## Not built yet, by plan

These are features the project intends to build but has not reached. Until they exist,
nothing in the repository claims they work.

| Area | Status | Next step |
| --- | --- | --- |
| Email or webhook delivery | The in-app channel is built on the outbox; no second channel exists | Reuse the same events and the same exactly-once constraint, and add a provider retry policy |
| Push for the unread badge | The badge polls every 20 seconds | Server-sent events, with the worker signalling the API through `LISTEN/NOTIFY` |
| Shareable saved views | Presets are component state, so a reload returns to "all work" | Put the structured filters in the URL and teach the router query strings |

## Deliberately excluded from scope

Each of these is a conscious decision, not an oversight. The brief allows a smaller system
with well-considered behaviour, and the guidance explicitly asks for several of these to be
deferred.

| Excluded | Why |
| --- | --- |
| Email or push notification delivery | Requires an external provider and credentials. The engineering problem — reliable, retry-safe secondary work — is solved by the outbox and the worker; only the transport is absent. In-app notifications are delivered. |
| File attachments | Object storage, virus scanning, size quotas and access control are a project of their own, and none of the brief's critical behaviours depend on them. |
| SSO / OIDC / SAML | Not required by the brief, which explicitly allows seeded demo users with a simple login. An external identity provider would add configuration the reviewer cannot run offline. |
| Real-time updates (WebSockets) | The brief says polling or refetching is sufficient. WebSockets would add a stateful connection layer and a second source of truth without improving any tested invariant. |
| Mobile applications | Out of scope for a browser-based internal tool. |
| Reopening closed items | Deliberately not implemented. Reopening must define who may do it, whether it resets the version, and what history it produces. Rather than guess, closing is terminal for now and the decision is recorded in `ENGINEERING_DECISIONS.md`. |
| Deleting work items and comments | Neither can be deleted through the API. Comments are append-only; work items are mutable and versioned, and closing is terminal. |
| Editing or deleting comments | A comment is evidence of what somebody said. Editing it would make the activity timeline unreliable. |
| Login rate limiting and account lockout | Not implemented. The mitigation today is a generic error message that does not reveal whether an email exists. See [`SECURITY.md`](SECURITY.md) for the residual risk. |
| Password change, password reset, MFA | There is no self-registration and no credential lifecycle in the demo scope. |
| Creating a brand-new user through the API | An admin may only grant a role to somebody they already share a team with, so the API can never be used to enumerate strangers. Adding a person who has no account yet is a seeded-data operation, not an endpoint |
| Signing out of every session at once | The service function exists (`revoke_all_for_user`) and the command is documented in `OPERATIONS.md`, but there is no endpoint for a user to do it themselves |

## Known technical limitations

| Limitation | Impact | How it would be addressed |
| --- | --- | --- |
| `alembic check` does not compare `CHECK` constraint bodies | A check constraint altered in a model but not in a migration would not be caught automatically | Explicit constraint tests cover the important ones; a schema-dump diff would close the gap |
| Enum-like values live in both a Python enum and a SQL `CHECK` | Adding a value needs a coordinated change, and a mismatch is only caught by tests | A test asserts each value set against the database; a generated constraint would remove the duplication |
| The test suite truncates a real database | Tests cannot run in parallel against the same database, and a misconfigured `TEST_DATABASE_URL` would destroy data | Documented prominently in `README.md` and `docs/OPERATIONS.md`; a per-worker schema or database would allow parallelism |
| Single API process, no shared connection pool | Horizontal scaling needs an external pooler (for example PgBouncer) | Stated as an evolution path, not a current capability |
| No measurements at the target scale | The brief's figures (tens of thousands of items, thousands of concurrent users) are design expectations. Query plans were inspected at 3 000 items, but no load test has been run and no throughput claim is made | Seed a few thousand items, inspect plans (done — see `docs/DATA_MODEL.md`), then load test against the brief's target |
| Deeper keyset pages walk past the entries they skip | The cursor predicate is applied as a filter after the index seek, so page 13 costs more than page 1 (301 index entries walked past, with no heap fetches for them). Still strictly better than `OFFSET`, which would also read those rows | A composite row comparison — `(created_at, id) < (…)` — would seek directly to the position. Measured and documented in `docs/DATA_MODEL.md` |
| Idempotency keys are never pruned automatically | The `idempotency_keys` table grows with every create and has no expiry. Bounded at demo scale, but unbounded in principle | A scheduled delete of keys older than the retry window, alongside the existing session housekeeping command |
| Search understands only the English text configuration | Title and description are indexed with `english` stemming, so a non-English workspace would match poorly | Store a configuration per team, or index with `simple` and normalise at query time |
| Any member may advance any item in their team | A member can move a colleague's item forward. This is intended — it is how a small team covers for one another — and it is asserted in a test rather than left implicit, but a stricter deployment might want assignee-only | Narrow `transition_work` in `app/policy/workflow.py` to the assignee or a lead; one rule plus its test |
| No page numbers and no total count | The cursor is a position, so a client cannot request "page 7" or get an item count; it steps forward from where it is | Deliberate: a total count on every request costs a full count for a number nobody acts on, and `has_more` answers the question clients actually ask |
| No observability beyond structured logs | Diagnosing a production incident would rely on logs alone | Add request IDs, metrics and tracing when the deployment target is known |
| The idle session window can be enforced up to ~60 s late | `last_used_at` is refreshed at most once a minute, so a session that has just gone idle takes effect slightly later | Deliberate: the alternative turns every read into a write. Documented in `ENGINEERING_DECISIONS.md`, with the interval as a tunable constant |
| A revoked or expired session is reported, but a stale role is not | A session carries no role of its own, so a role change applies from the next request rather than forcing a re-login. This is intended, not a defect | Documented in `SECURITY.md`; no change planned |
| The activity views are not text-searchable | Neither view has a `q`. The team feed API can be narrowed by date range and actor, but finding an old *note* still means paging to it, and an item's timeline has no filter at all. A thread is short and a timeline is read in context, so the cost is low | Add the same `websearch_to_tsquery` filter the item list uses, over the comment body |
| Activity rows are not pruned | `activity_events` grows with every mutation and is never archived. At demo scale it is thousands of rows; over years it would be the largest table | Partition by month, or archive closed items' history to cold storage |
| A page reload signs the user out | The token is held in memory only, so the credential dies with the page. Deliberate: it is the only storage option where closing a tab destroys the token | `sessionStorage` if the reload cost outweighs the shorter exposure; `HttpOnly` cookies with `SameSite=Strict` plus an anti-CSRF token is the real production answer |
| No member-management UI | The API supports adding, changing and removing members (admin only, with the last-admin and assigned-items rules enforced) but the interface has no screen for it. It is outside the work-item vertical slice | A team settings view; the endpoints and their tests already exist |
| Optimistic updates only for the claim | A claim shows immediately and is rolled back on failure; transitions and edits wait for the server. Deliberate — see `ENGINEERING_DECISIONS.md` 24 | An optimistic transition would need a rollback that shows a workflow state nobody else saw; not planned |
| The team activity UI has no actor picker | The API accepts an `actor` filter and the client's query builder carries it, but the feed view only exposes the date range | Add a member select that sets the existing `actor` query parameter |
| The unread badge lags by up to 20 seconds | It polls one indexed count instead of holding a push channel open. A notification page opened immediately after an assignment may not show it yet | The page states the delay; server-sent events would remove it |
| The item list has no sort control | The order is the server's `(created_at, id)` descending keyset order, which is what a keyset cursor requires. A different order needs a different cursor | A second ordered index and a sort parameter, with the order encoded in the cursor's kind |
| Frontend tests stub the network | They prove the components' behaviour, not the wire format. The wire format is covered by a separate contract check against a running API rather than by CI | A browser-driven end-to-end test (Playwright) against a seeded database, run in CI |
| The Docker stack is development-grade | Published database port, the documented demo password, no TLS, and no reverse proxy in front beyond nginx serving the SPA. It is a review environment, not a production deployment | TLS termination, a secret manager, and the CSP from `OPERATIONS.md` for a real deployment |
| The outbox worker is a single container | Two workers are safe (`SKIP LOCKED`, tested), but nothing supervises or alerts on a stopped worker beyond the health of the container itself | Run the worker under a supervisor with a liveness check; alert on the `failed` count from `GET /api/v1/ops/outbox` |
| No notification retention policy | `notifications` grows with every assignment, approval and rejection and is never pruned (except by the membership cascade) | Delete read notifications older than a window, the same way idempotency keys would be pruned |
| Seeding is additive, not converging | Editing the generator does not change rows that already exist, so a stale local dataset needs a schema rebuild | A converging seed (`ON CONFLICT DO UPDATE`) once the item generator is stable |

## Honest statement of scale

The application has not been load-tested. It is designed so that the parts that usually
break under concurrency — claims, version checks, duplicate submissions — are enforced by
PostgreSQL rather than by application timing, and so that list views never load a full
table into memory. That is a design argument, not a measurement, and it is presented as
such.
