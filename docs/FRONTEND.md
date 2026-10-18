# Frontend

The React client for the operations API: what it does, how it decides things, and what it
deliberately does not do. It is a single-page application with five views and no server-side
rendering, because the API is the only source of truth and the client's job is to show it.

```
frontend/
  src/
    api/          the HTTP client and the response types
    auth/         the session store, sign-in and sign-out
    components/   the shell and the four view states
    features/     one directory per feature area: items, notifications, activity
    lib/          formatting, error explanations, the router, the role matrix
    query/        the TanStack Query client, its keys and its invalidation helpers
    test/         the fake network, fixtures and render helpers
```

## The views

| Route | View |
| --- | --- |
| `#/teams/:teamKey` | The team's work items: presets, filters, search, cursor paging, create |
| `#/teams/:teamKey/items/:itemId` | One item: fields, available moves, discussion, history |
| `#/teams/:teamKey/activity` | The team's activity feed |
| `#/my-work` | Everything assigned to the caller, across every team |
| `#/notifications` | The caller's notifications, with an unread count |

The first three are **team-scoped**: the team key comes from the URL and is checked against the
caller's own memberships before any request is made. The last two are **person-scoped**: there
is no team in the path because the scope is the token. That split mirrors the API, where
`/me/items` and `/notifications` derive their scope from the session rather than from a
parameter.

Four of the team's "what needs attention?" questions are one click, not a manual filter
combination: all work, assigned to me, unassigned urgent, and awaiting approval. Each preset
sets the structured filters that the list already sends to the server, so the resulting request
is the same one the checkboxes would have produced, over the whole dataset rather than the
loaded page. The selected preset is derived from the filters, ignoring the search box, so
typing a search term does not clear the selection.

Routing is a hash router of about sixty lines (`src/lib/router.ts`). It is a pure
`parseHash` function plus a `useSyncExternalStore` subscription, which means the routing rules
are tested without a DOM. A routing library was not used because there is one nested route and
no data loaders: the two things a router library is actually for.

Hash routing rather than the History API because it needs no server rewrite rule, so the built
`dist/` can be served by any static file server.

## Server state is the only state

Every list, item, comment and event on screen came from a response. Nothing is derived from a
locally maintained copy, and nothing is filtered or sorted in the browser.

- **Filtering, searching and paging are query parameters.** Changing a filter changes the
  query *key*, which is a new request. The browser never holds a whole team's items, so
  "show me every unassigned urgent item" is correct rather than "…that happens to be in the
  page I already loaded".
- **Paging is the server's cursor.** `next_cursor` is handed back verbatim; `has_more` decides
  whether the "Load more" control exists at all.
- **A claim is optimistic; everything else is server-confirmed.** Clicking *Claim* shows the
  item as yours immediately, because the change is one field and the likely failure — somebody
  else got there first — has a clear reconciliation. The provisional state is announced on
  screen, the transition buttons are disabled while it is in flight, and a `409` restores the
  snapshot and then re-reads the item, so the page ends up showing the winner rather than the
  guess. Transitions and edits still wait for the server: an optimistic `awaiting_approval` that
  came back `412` would show a workflow state that never existed. The rollback is covered by
  three tests, not assumed.
- **A successful write is adopted, not re-fetched.** The mutation's response *is* the new state,
  so it goes into the item's cache entry directly. That entry holds the item **and its `ETag`**
  together, so the version a conditional write is conditioned on is always the version of the
  item it is writing to. This includes the `ETag` a **claim** returns: claiming bumps the
  version, and discarding that header is what would make the next transition fail with `412`.
  A refused write (`409`, `412`) re-reads the item instead, because the local copy is then known
  to be stale.
- **Notifications are polled, not pushed.** The unread badge asks for one indexed count every
  20 seconds. Delivery is asynchronous, so the badge can lag the change by a moment; the
  notification page says so rather than implying a live feed. There is no WebSocket, which the
  brief rules out, and no server-sent events, which would need the worker to signal the API
  process.
- **A `401` from any request ends the session.** One handler is registered on both the query and
  mutation caches, so an expired token returns the user to the sign-in screen rather than leaving
  a page showing an error it can never recover from.

## The activity feed says what it is about

History is one component (`features/items/ActivityTimeline.tsx`) used twice. An item's page
already identifies the item, so its rows read "Nina Petrova claimed this item". A team feed
spans many items, so the page passes `itemHref` and each row names the item's **current** title
as a link instead — otherwise the feed is a wall of "this item" rows with no way to tell them
apart. The title is joined by the server at read time; the browser does not look items up.

The feed is filterable by date range in the UI, and the API also accepts an `actor` query
parameter. This view does not expose an actor control yet; the client's query builder can carry
one. The date range is part of the query key, so changing it starts a new first page rather
than filtering what is already loaded. "To 18 Oct" is sent as the last millisecond of that local
day: a range control that silently drops its final day would be wrong, so the bounds are
inclusive.

The rail beside each row carries a three-word legend — approved, sent back, status change. The
text of the row says the same thing, so the colour is a reading aid rather than the only signal.

A failed *next* page is not a failed view: the rows already read stay on screen, and an inline
retry re-requests that one page with `fetchNextPage` rather than `refetch`, which on an
infinite query would re-walk every page loaded so far. Both behaviours are covered by
`features/activity/TeamActivityPage.test.tsx`, including a three-page walk where the retry is
asserted to be a single request carrying the failed page's cursor.

## Retries, and why only two mutations retry

Retrying a write is only safe when the server can recognise the retry. The default for
mutations is therefore **no retry**, and exactly two opt in:

| Mutation | Retries | Why |
| --- | --- | --- |
| Create a work item | Yes (2) | Carries an `Idempotency-Key`; a retry replays the first item |
| Post a comment | Yes (2) | Same mechanism, same guarantee |
| Claim | No | A successful claim returns the current state; if somebody else won, a retry is `409`. The UI shows the server confirmation rather than retrying the action |
| Transition | No | A retry after a success would come back `412` — the version moved |
| Edit | No | Same |

The key is a **mutation variable**, generated once when the user acts
(`src/lib/idempotency.ts`), not inside the request function. That is the whole reason the retry
is safe: every attempt is the same logical operation. Generating it per attempt would turn a
network blip into a duplicate work item, and `CommentThread.test.tsx` asserts the key is
identical across a retry specifically because that is the property a refactor would break
silently.

Queries retry only transport and `5xx` faults. A `4xx` will not improve on its own, and retrying
an authentication failure just delays the sign-in screen.

## The role matrix is advisory here

`src/lib/permissions.ts` mirrors `backend/app/policy/roles.py` so the interface does not offer a
control the server will refuse. It is **not** the boundary. Every write is authorized again on
the server, and the moves an item offers come from the server's own `available_transitions`,
computed for the caller's role. A client that ignored the matrix entirely would still be
correctly refused — it would simply be a worse interface.

Two examples of the interface rendering the server's decision rather than making its own:

- A viewer gets no claim button, no transition buttons and a read-only detail panel, because the
  server would answer `403` for each of them.
- The assignee picker is the team's roster (`/teams/{id}/members`), not a global directory,
  because work can only be assigned to a member of the item's own team — a rule the database
  enforces with a composite foreign key.

## Errors are explained, not printed

`src/lib/errors.ts` maps the API's stable `code` onto a title, a sentence, and whether reloading
could possibly help. The distinction matters:

- `412 stale_version` → "Somebody else changed this first… the change was not applied", with a
  **Reload the item** button, because the item on screen is genuinely out of date.
- `403 forbidden` → "Your role in this team does not permit that", with **no** reload button,
  because reloading changes nothing.
- `409 item_already_claimed` → "Already taken", with a reload, because the item now has an owner
  the page is not showing.

The server's own message is the fallback, never the primary source: a code is stable, a message
is prose.

## Tests

`npm run test` runs 83 tests in ten files. They render real components against a real
TanStack Query client and a **stubbed `fetch`** (`src/test/fetch.ts`) rather than mocking the API
module, so the error envelope, the `ETag` header and the query parameters are all covered. Every
request is recorded, which is how the tests assert on what the *server* was asked for: that a
filter became a query parameter, that a conditional write carried `If-Match`, that a retry
reused its key.

| File | Covers |
| --- | --- |
| `api/client.test.ts` | The error envelope, `ETag` capture, the bearer header, filter parameter shapes |
| `lib/router.test.ts` | Route parsing, including the shapes that must be refused |
| `lib/errors.test.ts` | Conflict wording, and that a role refusal does not promise a reload |
| `auth/LoginPage.test.tsx` | Refusal wording, session installation, the demo-account buttons |
| `features/items/ItemListPage.test.tsx` | Loading, empty, error and retry; filters and presets as server queries; cursor paging |
| `features/items/MyWorkPage.test.tsx` | The cross-team scope, no `assignee` parameter, per-row team links, presets, paging |
| `features/items/ItemDetailPage.test.tsx` | The role boundary; optimistic claim, its rollback and its `ETag`; `412` handling |
| `features/notifications/NotificationsPage.test.tsx` | The list, the unread count, acknowledging one and all, a failed acknowledgement, scoping |
| `features/items/CommentThread.test.tsx` | Idempotency-key reuse across a retry, length limits, viewer read-only |
| `features/activity/TeamActivityPage.test.tsx` | Item title and link per row, inclusive date bounds as server parameters, a failed next page retried once |

What the frontend tests do **not** prove is the wire format — they stub the network. That is
covered separately by a contract check that sends the same requests to a running API; see the
Phase 6 section of `IMPLEMENTATION_STATUS.md`.

## Known frontend limitations

- **A page reload signs the user out.** Deliberate: the token is in memory only. See
  `SECURITY.md`.
- **Optimistic updates only for the claim.** Deliberate; see decision 24. Edits and transitions
  wait for the server.
- **The selected preset is not in the URL.** A reload returns to "all work". Making a view
  shareable needs query-string routing, which is not built.
- **The unread badge can be up to 20 seconds stale.** That is the poll interval, and it is a
  deliberate alternative to a push channel.
- **No member-management UI.** The API supports it (admin only); the interface does not, because
  the vertical slice is about work items.
- **No offline support and no service worker.** The API is authoritative; a cached page would be
  a page showing data the server has moved past.
- **The item list has no sort control.** The order is the server's `(created_at, id)` descending
  keyset order, which is the order a keyset cursor requires.
