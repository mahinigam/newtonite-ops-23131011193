# Security Model

What the application protects, how, and what it does not yet protect. Written to be read
alongside the code; every claim here is either enforced in the source or tested in
`backend/tests/`.

## Assets and trust boundaries

| Asset | Where it lives | Consequence if disclosed |
| --- | --- | --- |
| Passwords | Argon2id digests in `users.password_hash` | Offline guessing against a memory-hard KDF |
| Session tokens | SHA-256 digests in `auth_sessions.token_hash` | Nothing usable — the raw token is not stored anywhere |
| Work items, comments, activity | PostgreSQL | Cross-team data exposure if authorization is wrong |
| Memberships and roles | PostgreSQL | Privilege escalation if a client value were trusted |

The trust boundary is the API. The browser, an API client and anything else on the network are
untrusted; PostgreSQL and the API process are trusted.

## Authentication

- **Passwords** are hashed with Argon2id via `argon2-cffi` (`app/security/passwords.py`), which
  generates a per-password salt and embeds the parameters in the digest, so the cost can be
  raised later without a schema change. Plaintext is never stored, logged or returned.
- **Session tokens** are 256 bits from `secrets.token_urlsafe`. Only the SHA-256 digest is
  stored, and lookup is by digest, so the raw token exists in exactly one place: the client
  that received it.
- **Why the two hashes differ.** A random 256-bit token cannot be brute-forced, so a slow KDF
  would add latency to every authenticated request for no security gain. A human password can
  be guessed offline, so it gets a memory-hard KDF. This is a deliberate asymmetry, not an
  inconsistency.
- **Unknown accounts take a similar amount of time as a wrong password.** The no-such-user path
  calls `spend_dummy_verification()`, which performs a real Argon2id verification against a
  throwaway digest, so response time does not reveal whether an address has an account. The
  response body is identical in both cases, and a test asserts the two are byte-identical.
- **Sessions expire twice.** An absolute lifetime (12 h from login) and an idle lifetime (2 h
  without use). Both comparisons are evaluated with PostgreSQL's `now()`, so two API processes
  cannot disagree about whether a session is still valid.
- **Revocation is immediate.** Logout sets `revoked_at`; every subsequent request that presents
  the token is refused. The row is kept rather than deleted, so a stolen token cannot be
  resurrected by recreating a session with the same digest.

## Authorization

- Roles are per team and are read from the database on every request. A `team_id`, `role` or
  `user_id` supplied by a client is never consulted for a permission decision, and request
  models use `extra="forbid"` so such a field is rejected outright rather than ignored.
- The matrix lives in one place (`app/policy/roles.py`) and is applied by a FastAPI dependency,
  so a route cannot perform an action before checking it, and a route that forgot its check is
  visible in review.
- **Non-members receive `404`, not `403`**, so responses cannot be used to confirm that another
  team's resource exists. `403` is reserved for a caller already inside the team. A test asserts
  a foreign team and a random UUID produce identical responses.
- Writes return what the database now holds, never an echo of the request.
- **Notifications are scoped to a person, not a team.** The query is
  `recipient_id = <the caller>`; there is no team in the path and no `user_id` parameter, so
  the scope is the token. Somebody else's notification id is a `404`, exactly like a foreign
  item, and the same database composite foreign key that keeps an assignee inside the team
  keeps a recipient inside the team.
- **`GET /ops/outbox` is scoped to teams where the caller is an admin** and returns counts
  only — no titles, recipients or payloads. There is no global operator role in this domain,
  and the endpoint does not invent one.
- **The worker has no HTTP surface.** It holds a database connection and nothing else; it is
  not reachable from the browser, and it authorizes nothing — every recipient was chosen and
  constrained when the event was written.

## Injection and input handling

- All SQL goes through SQLAlchemy, which binds values as parameters. There is no string
  concatenation of user input anywhere. The single place a literal is interpolated into a
  fragment is `_interval(unit, quantity)` in `app/services/auth.py`, where `unit` comes from
  the module's own source (`"second"`, `"hour"`) and `quantity` is still a bound parameter.
- The database cannot be reached from the browser. All filtering, searching and pagination
  happen in SQL; the frontend sends parameters, not queries.
- Every request body is validated by a Pydantic model with an explicit field allow-list.
- Error responses contain a stable code and a human message. Stack traces, SQL, table names and
  constraint names never reach a client; unexpected exceptions are logged server-side and
  answered with a generic body.

## XSS, token storage in the browser, and CSRF

This is the area where it is easiest to make a claim that is not true, so it is stated
precisely.

**The token is a bearer credential.** Any party who reads it can act as that user until the
session expires or is revoked. Therefore the security of the token is the security of the page
that holds it.

**Where it is stored.** The token lives in a module variable in `frontend/src/api/client.ts`
and is **never written to `localStorage` or `sessionStorage`**. The cost is real and is stated
rather than glossed over: **a page reload signs the user out.**

Both web-storage options are readable by JavaScript, so *neither defends against XSS* — the
choice is only about how long a stolen token stays usable. `localStorage` persists until it is
explicitly cleared, so a token captured once survives browser restarts. `sessionStorage` narrows
that to the tab's lifetime. Keeping it in memory narrows it further, to the lifetime of the page,
and is the only one of the three where closing a tab is sufficient to destroy the credential.

The price is a reload. The session is already short-lived (12 h absolute, 2 h idle) and revocable,
and the demo accounts are one click away on the sign-in screen, so the reload is the cheaper of
the two prices. A different application — one where a lost page means lost work — could
reasonably choose `sessionStorage` instead; the trade-off is recorded in
`ENGINEERING_DECISIONS.md` rather than presented as a free win.

**What XSS would actually allow.** Injected script running on the application's origin could
read the in-memory token, and — more to the point — could simply issue authenticated requests
without ever reading it. So the real defence is not token storage; it is:

1. **Never render untrusted text as HTML.** Comment bodies, titles and descriptions are
   inserted as text nodes by React. There is no `dangerouslySetInnerHTML` anywhere in the
   frontend and no HTML sanitiser to get wrong, and no `meta` value from the activity feed is
   interpreted as markup.
2. **A short session lifetime and immediate revocation**, which bound the damage and make
   "sign out everywhere" meaningful.
3. **A strict `Content-Security-Policy` in a real deployment**, which would block inline and
   remote script. The repository does not ship a CSP today: `OPERATIONS.md` recommends the
   policy, and `KNOWN_LIMITATIONS.md` records it as deployment hardening. It would complement,
   not replace, the two controls above.

**Why not an `HttpOnly` cookie, which XSS cannot read?** Because a cookie sent automatically by
the browser introduces CSRF, and defending against CSRF requires a token, `SameSite`
configuration and care with cross-origin requests. The trades are:

| Approach | XSS reads the credential | CSRF possible |
| --- | --- | --- |
| Bearer token in memory (this project) | Only while the page is open | No — the browser does not attach it automatically, and a cross-site form cannot set an `Authorization` header |
| Bearer token in `sessionStorage` | Until the tab closes | No, for the same reason |
| `HttpOnly` cookie | No | Yes, unless mitigated |

Neither is strictly better, and this project chose the one whose weakness is mitigated by
controls it actually implements (no HTML injection, short sessions and immediate revocation)
over the one whose weakness requires additional machinery. A production deployment with a
high-value threat model should consider `HttpOnly` cookies with `SameSite=Strict` plus an
anti-CSRF token, and should add the CSP described above.

**Cross-origin requests are restricted, not assumed.** The API allows only the configured
origins (`CORS_ALLOWED_ORIGINS`, comma-separated; default `http://localhost:5173`) and only
the headers the application uses — `Authorization`, `Content-Type`, `Idempotency-Key`,
`If-Match`. `allow_credentials` is `false`, which is correct for a bearer token that the
browser must be told to send. The Vite dev server is configured with `strictPort: true` so a
port clash fails loudly instead of silently moving the browser to an origin the API does not
allow.

**No CSRF tokens are required** with the current design, precisely because the browser never
attaches the credential on its own.

## Residual risks, stated plainly

| Risk | Current state | Mitigation if time allowed |
| --- | --- | --- |
| No login rate limiting or lockout | An attacker may attempt passwords without limit. Each attempt costs an Argon2id verification, which slows but does not prevent offline-speed guessing against a captured hash, and does not prevent distributed online guessing | Per-account and per-IP attempt counters with backoff, and alerting on spikes |
| Token readable by script on the origin while the page is open | See above. The in-memory choice removes the *persistence*, not the exposure | `HttpOnly` cookies with `SameSite=Strict` and an anti-CSRF token, or a tighter CSP with no third-party script |
| A page reload ends the session | Deliberate consequence of in-memory storage: the user signs in again | `sessionStorage` if the reload cost outweighs the shortened exposure |
| No MFA | Password-only | TOTP for admin and lead roles at minimum |
| No password change, reset or rotation | Credentials are fixed by the seed; there is no user-creation endpoint, so there is nothing to reset yet | Password change with current-password confirmation, and a reset flow that invalidates existing sessions |
| Sessions are not rotated on privilege change | A session carries no role of its own, so a role change takes effect on the next request rather than requiring a new login | That behaviour is arguably already correct; document it explicitly |
| Members can read all data in their team | Any member, including a viewer-beyond-role, sees the full roster | Field-level visibility rules if a future requirement demands them |
| Idle window enforced up to ~60 s late | `last_used_at` is refreshed at most once a minute to avoid turning every read into a write | Tighten the interval, or compare against the request's start time |
| A notification body is a snapshot | It contains the item title as it was when the event was written, so a later rename is not reflected. It is visible only to a current member of the team, and leaving the team deletes it | Render the item live and keep only the event type and ids in the row; the cost is a join on every notification |
| Queue rows are readable by anyone with database access | The payload holds an item title and a recipient id, like every other table. The API never returns payloads | Column-level grants or encryption at rest, as part of a real deployment |

## Reporting

This is an assessment project, not a deployed service. There is no production data and no real
credentials in the repository; the demo accounts in `README.md` are documented development
placeholders.
