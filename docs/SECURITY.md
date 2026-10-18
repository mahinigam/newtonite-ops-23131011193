# Security Model

What the application protects, how, and what it does not yet protect. Written to be read
alongside the code; every claim here is either enforced in the source or tested in
`backend/tests/`.

## Assets and trust boundaries

| Asset | Where it lives | Consequence if disclosed |
| --- | --- | --- |
| Passwords | Argon2id digests in `users.password_hash` | Offline guessing against a memory-hard KDF |
| Access tokens (short-lived, 5 min) | In-memory only — never written to storage | Credential usable only while the page is open |
| Refresh tokens (long-lived, 7 days) | HttpOnly, Secure, SameSite=Strict cookie scoped to `/api/v1/auth/refresh` | XSS cannot read it; CSRF prevented by SameSite |
| Work items, comments, activity | PostgreSQL | Cross-team data exposure if authorization is wrong |
| Memberships and roles | PostgreSQL | Privilege escalation if a client value were trusted |

The trust boundary is the API. The browser, an API client and anything else on the network are
untrusted; PostgreSQL and the API process are trusted.

## Authentication

- **Passwords** are hashed with Argon2id via `argon2-cffi` (`app/security/passwords.py`), which
  generates a per-password salt and embeds the parameters in the digest, so the cost can be
  raised later without a migration.
- **Access tokens** are 256 bits of `secrets` randomness, base64url-encoded, stored only as
  a SHA-256 digest (`app/security/tokens.py`). A database disclosure does not hand an attacker
  usable credentials. They are short-lived (5 minutes by default) and carried in the
  `Authorization: Bearer` header.
- **Refresh tokens** use the same 256-bit random scheme, stored as SHA-256 digests in the
  `refresh_tokens` table. They are delivered via an `HttpOnly; Secure; SameSite=Strict` cookie
  scoped to `/api/v1/auth/refresh`, so JavaScript cannot read them and they are not sent to
  any other endpoint. They are long-lived (7 days by default).
- **Token rotation with reuse detection:** each refresh token is single-use. When `/auth/refresh`
  is called, the presented token is revoked and a new pair is issued. If a revoked token is
  presented again (reuse attack), the entire rotation chain is revoked — both the attacker
  and the legitimate user are logged out.
- **Session validity** is checked against PostgreSQL on every request: token digest, not
  revoked, not expired, user still active. The database clock is the authority.
- **Idle window:** access tokens are refreshed at most once per minute per session, so the
  idle window (2 hours by default) is enforced up to a minute late. Recorded in
  `KNOWN_LIMITATIONS.md`.

**Token storage strategy.** The access token lives in JavaScript memory only — never in
`localStorage` or `sessionStorage` — so an XSS bug cannot steal a credential that outlives
the tab. The refresh token is an HttpOnly cookie that the browser sends automatically to the
refresh endpoint; it is never visible to JavaScript. This combination gives the user persistence
across page reloads without exposing either token to script.

**Why not an `HttpOnly` cookie for the access token?** Because a cookie sent automatically
introduces CSRF, and defending against CSRF requires a token, `SameSite` configuration and
care with cross-origin requests. The trades are:

| Approach | XSS reads the credential | CSRF possible |
| --- | --- | --- |
| Access token in memory + refresh in HttpOnly cookie (this project) | Only while the page is open | No — the browser does not attach the access token automatically, and the refresh cookie is scoped to one endpoint |
| Access token in `sessionStorage` | Until the tab closes | No, for the same reason |
| `HttpOnly` cookie for access token | No | Yes, unless mitigated |

**What XSS would actually allow.** Injected script running on the application's origin could
read the in-memory access token, and — more to the point — could simply issue authenticated
requests without ever reading it. So the real defence is not token storage; it is:

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

**Cross-origin requests are restricted, not assumed.** The API allows only the configured
origins (`CORS_ALLOWED_ORIGINS`, comma-separated; default `http://localhost:5173`) and only
the headers the application uses — `Authorization`, `Content-Type`, `Idempotency-Key`,
`If-Match`. `allow_credentials` is `true` (required for the refresh cookie) but the origin
list is explicit and never wildcards. The Vite dev server is configured with `strictPort: true`
so a port clash fails loudly instead of silently moving the browser to an origin the API does
not allow.

**No CSRF tokens are required** with the current design for the access token, precisely because
the browser never attaches it on its own. The refresh cookie is scoped to `/api/v1/auth/refresh`
and sent only to that endpoint, which limits the CSRF surface to a single POST that returns
a new token pair (not a state change). A production deployment with a high-value threat model
should consider adding an anti-CSRF token to the refresh endpoint as well.

## Authorization

- **Roles are per-team, not global.** A user's role in team A says nothing about their role
  in team B. The only place a role exists is `memberships`.
- **The matrix lives in one place** (`app/policy/roles.py`) and is applied as a dependency,
  so a route cannot perform an action before checking it.
- **The team boundary is derived from the resource, not the request.** Every item read/write
  loads the item and checks membership against `row.team_id`. A cross-team id is a `404`,
  indistinguishable from a missing one.
- **Assignee eligibility is a database constraint.** The composite foreign key
  `work_items(team_id, assignee_id) → memberships(team_id, user_id)` ensures an assignee
  is a member of the item's team, and it is enforced even against code paths nobody has written.
- **Memberships are non-escalating.** An admin may only set a role for somebody who already
  shares a team with them; they cannot enumerate strangers.

## What is deliberately not protected

- **No rate limiting on login.** The generic error message does not reveal whether an email
  exists, but an attacker can still try passwords. Listed in `KNOWN_LIMITATIONS.md`.
- **No account lockout.** Same reason.
- **No CSP shipped.** The recommended policy is in `OPERATIONS.md`; it is a deployment step.
- **No password change or reset flow.** Demo scope; no credential lifecycle endpoints.
- **No MFA.** Out of scope for the demo.
