"""Authentication: login, token pair issuance, rotation, revocation and session resolution.

Token model
-----------
A login returns a **token pair**:

1. **Access token** (short-lived, default 5 min) — carried in the
   `Authorization: Bearer` header. Only its SHA-256 digest is stored in
   `auth_sessions`. Validated on every authenticated request.

2. **Refresh token** (long-lived, default 7 days) — delivered via an
   `HttpOnly; Secure; SameSite=Strict` cookie scoped to `/api/v1/auth/refresh`.
   Only its SHA-256 digest is stored in `refresh_tokens`. Used solely to obtain
   new access tokens.

Both tokens are rejected when any of the following holds:

* the digest is unknown (the token was never issued, or was tampered with),
* `revoked_at` is set (logout, rotation, or reuse detection),
* `expires_at` has passed (absolute lifetime),
* for access tokens only: `last_used_at` is older than the idle window.

Two clocks, one authority
-------------------------
Every expiry comparison is evaluated against PostgreSQL's `now()`, never the
application's clock. With more than one API process, or a container whose clock
drifts, an application-side comparison would let a session live longer on one
process than another. The database is the single clock.

Refresh token rotation & reuse detection
----------------------------------------
Each refresh token is single-use. When `/auth/refresh` is called:

1. The presented refresh token is looked up by its digest.
2. If it is valid (not revoked, not expired), it is **revoked** and a new token
   pair is issued. The new refresh token stores the hash of the one it replaced
   (`replaced_by_token_hash`), forming a rotation chain.
3. If a revoked token is presented again (token reuse), the entire rotation
   chain is revoked — the attacker and the legitimate user are both logged out.
   This is the standard "rotating refresh tokens with reuse detection" pattern.

Why `last_used_at` is not written on every request
--------------------------------------------------
Updating it per request would turn every read into a write. Instead the update is
conditional - it only happens once the stored value is more than
``TOUCH_INTERVAL_SECONDS`` old - so the column is refreshed at most once a minute
per session. The cost is that the idle window can be enforced up to a minute late,
which is recorded in `KNOWN_LIMITATIONS.md` rather than left implicit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Integer, bindparam, delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from app.config import Settings
from app.errors import InvalidCredentials, InvalidSession, SessionExpired
from app.models import AuthSession, Membership, RefreshToken, User
from app.models.enums import Role
from app.policy.principal import Principal
from app.policy.roles import parse_role
from app.security.passwords import spend_dummy_verification, verify_password
from app.security.tokens import generate_refresh_token, generate_token, hash_token

# How stale `last_used_at` is allowed to get before it is refreshed.
TOUCH_INTERVAL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    access_expires_at: datetime
    access_session_id: uuid.UUID
    refresh_token: str
    refresh_expires_at: datetime
    refresh_session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Legacy single-token session, kept for compatibility during migration."""

    token: str
    expires_at: datetime
    session_id: uuid.UUID


def _interval(unit: str, quantity: int) -> TextClause:
    """`interval '1 <unit>' * :quantity`, parameterised.

    `unit` is always a literal from this module ('second' or 'hour'), never
    caller-supplied, so interpolating it is not an injection risk; `quantity`
    stays a bound parameter. The bind name is derived from both, so a statement
    can safely mix intervals of different lengths.
    """
    name = f"{unit}_{quantity}"
    return text(f"interval '1 {unit}' * :{name}").bindparams(
        bindparam(name, value=quantity, type_=Integer)
    )


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    settings: Settings,
) -> tuple[User, TokenPair]:
    """Verify credentials and start a token pair.

    Raises `InvalidCredentials` for an unknown email, a wrong password and a
    disabled account alike, so the response cannot be used to discover which
    addresses have accounts.
    """
    normalised = email.strip().lower()
    user = (
        await session.execute(select(User).where(User.email == normalised))
    ).scalar_one_or_none()

    if user is None:
        # Spend comparable time to a real verification so the response time does
        # not reveal whether the account exists.
        spend_dummy_verification()
        raise InvalidCredentials()

    if not verify_password(password, user.password_hash):
        raise InvalidCredentials()

    if not user.is_active:
        raise InvalidCredentials()

    # Issue access token (short-lived)
    access_token = generate_token()
    access_stmt = (
        insert(AuthSession)
        .values(
            user_id=user.id,
            token_hash=hash_token(access_token),
            expires_at=func.now() + _interval("minute", settings.access_token_ttl_minutes),
            last_used_at=func.now(),
        )
        .returning(AuthSession.id, AuthSession.expires_at)
    )
    access_row = (await session.execute(access_stmt)).one()

    # Issue refresh token (long-lived)
    refresh_token = generate_refresh_token()
    refresh_stmt = (
        insert(RefreshToken)
        .values(
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            expires_at=func.now() + _interval("day", settings.refresh_token_ttl_days),
        )
        .returning(RefreshToken.id, RefreshToken.expires_at)
    )
    refresh_row = (await session.execute(refresh_stmt)).one()

    await session.execute(update(User).where(User.id == user.id).values(last_login_at=func.now()))
    await session.commit()

    return user, TokenPair(
        access_token=access_token,
        access_expires_at=access_row.expires_at,
        access_session_id=access_row.id,
        refresh_token=refresh_token,
        refresh_expires_at=refresh_row.expires_at,
        refresh_session_id=refresh_row.id,
    )


async def authenticate(session: AsyncSession, *, token: str, settings: Settings) -> Principal:
    """Resolve an access token to a `Principal`, or raise.

    Fails with `SessionExpired` when the session existed but timed out, and
    `InvalidSession` otherwise. The distinction is safe to expose - the caller
    already holds the token - and lets the frontend say "please sign in again"
    rather than "something is wrong".
    """
    digest = hash_token(token)

    row = (
        await session.execute(
            select(AuthSession.id, AuthSession.user_id, AuthSession.revoked_at)
            .join(User, User.id == AuthSession.user_id)
            .where(
                AuthSession.token_hash == digest,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > func.now(),
                AuthSession.last_used_at
                > func.now() - _interval("hour", settings.session_idle_ttl_hours),
                User.is_active.is_(True),
            )
        )
    ).one_or_none()

    if row is None:
        raise await _explain_failure(session, digest, settings)

    await _touch(session, row.id, settings)

    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one()
    return Principal(user=user, session_id=row.id, roles=await _roles_for(session, user.id))


async def revoke(session: AsyncSession, *, session_id: uuid.UUID) -> bool:
    """Sign out by revoking the access session.

    Returns False when the session was already revoked.

    Deleting the row would be simpler, but revocation is recorded instead so the
    session's history survives and a stolen token cannot be resurrected by
    recreating the row.
    """
    revoked_ids = (
        (
            await session.execute(
                update(AuthSession)
                .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
                .values(revoked_at=func.now())
                .returning(AuthSession.id)
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    return bool(revoked_ids)


async def revoke_all_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Revoke every live access session for a user."""
    revoked_ids = (
        (
            await session.execute(
                update(AuthSession)
                .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
                .values(revoked_at=func.now())
                .returning(AuthSession.id)
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    return len(revoked_ids)


async def revoke_refresh_token(session: AsyncSession, *, token: str) -> bool:
    """Revoke a refresh token by its plaintext value."""
    digest = hash_token(token)
    revoked_ids = (
        (
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.token_hash == digest, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=func.now())
                .returning(RefreshToken.id)
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    return bool(revoked_ids)


async def revoke_all_refresh_tokens_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Revoke every live refresh token for a user."""
    revoked_ids = (
        (
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=func.now())
                .returning(RefreshToken.id)
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    return len(revoked_ids)


async def delete_expired_sessions(session: AsyncSession) -> int:
    """Housekeeping: drop access sessions that are long past their absolute expiry."""
    removed = (
        (
            await session.execute(
                delete(AuthSession)
                .where(AuthSession.expires_at < func.now() - _interval("hour", 24 * 7))
                .returning(AuthSession.id)
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    return len(removed)


async def delete_expired_refresh_tokens(session: AsyncSession) -> int:
    """Housekeeping: drop refresh tokens that are long past their absolute expiry."""
    removed = (
        (
            await session.execute(
                delete(RefreshToken)
                .where(RefreshToken.expires_at < func.now() - _interval("hour", 24 * 7))
                .returning(RefreshToken.id)
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    return len(removed)


async def rotate_refresh_token(
    session: AsyncSession,
    *,
    refresh_token: str,
    settings: Settings,
) -> TokenPair:
    """Validate a refresh token, revoke it, and issue a new token pair.

    Implements rotating refresh tokens with reuse detection:

    1. Look up the presented token by hash.
    2. If not found, revoked, or expired → `InvalidSession`.
    3. If the token was already replaced (reuse detection), revoke the entire
       rotation chain (all tokens descended from the original) and raise
       `InvalidSession`.
    4. Otherwise, revoke the presented token, record its hash in the new
       token's `replaced_by_token_hash`, and issue a fresh token pair.
    """
    digest = hash_token(refresh_token)

    # Look up the refresh token with its user
    row = (
        await session.execute(
            select(
                RefreshToken.id,
                RefreshToken.user_id,
                RefreshToken.revoked_at,
                RefreshToken.replaced_by_token_hash,
                User.is_active,
            )
            .join(User, User.id == RefreshToken.user_id)
            .where(RefreshToken.token_hash == digest)
        )
    ).one_or_none()

    if row is None:
        raise InvalidSession()

    if row.revoked_at is not None or not row.is_active:
        # Reuse detection: if this token was already revoked, check whether it
        # was replaced by another token (i.e., the legitimate user rotated it).
        # If so, the attacker presented a stale token → revoke the entire chain.
        if row.replaced_by_token_hash is not None:
            await _revoke_refresh_chain(session, row.replaced_by_token_hash)
        raise InvalidSession()

    # Valid token — revoke it and issue a new pair
    await session.execute(
        update(RefreshToken).where(RefreshToken.id == row.id).values(revoked_at=func.now())
    )

    # Issue new access token
    new_access_token = generate_token()
    access_stmt = (
        insert(AuthSession)
        .values(
            user_id=row.user_id,
            token_hash=hash_token(new_access_token),
            expires_at=func.now() + _interval("minute", settings.access_token_ttl_minutes),
            last_used_at=func.now(),
        )
        .returning(AuthSession.id, AuthSession.expires_at)
    )
    access_row = (await session.execute(access_stmt)).one()

    # Issue new refresh token, linking it to the one it replaces
    new_refresh_token = generate_refresh_token()
    refresh_stmt = (
        insert(RefreshToken)
        .values(
            user_id=row.user_id,
            token_hash=hash_token(new_refresh_token),
            expires_at=func.now() + _interval("day", settings.refresh_token_ttl_days),
            replaced_by_token_hash=digest,  # chain the rotation
        )
        .returning(RefreshToken.id, RefreshToken.expires_at)
    )
    refresh_row = (await session.execute(refresh_stmt)).one()

    await session.commit()

    return TokenPair(
        access_token=new_access_token,
        access_expires_at=access_row.expires_at,
        access_session_id=access_row.id,
        refresh_token=new_refresh_token,
        refresh_expires_at=refresh_row.expires_at,
        refresh_session_id=refresh_row.id,
    )


async def _revoke_refresh_chain(session: AsyncSession, replaced_hash: str) -> None:
    """Revoke all refresh tokens in a rotation chain starting from `replaced_hash`.

    Called when a revoked token is presented again (reuse attack). The chain is
    followed via `replaced_by_token_hash` and every token in it is revoked.
    """
    # Recursive CTE to find all tokens in the chain
    chain_cte = (
        select(RefreshToken.id, RefreshToken.token_hash)
        .where(RefreshToken.token_hash == replaced_hash)
        .cte(name="revocation_chain", recursive=True)
    )
    chain_cte = chain_cte.union_all(
        select(RefreshToken.id, RefreshToken.token_hash).join(
            chain_cte, RefreshToken.replaced_by_token_hash == chain_cte.c.token_hash
        )
    )

    # Revoke all tokens in the chain
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.id.in_(select(chain_cte.c.id)))
        .values(revoked_at=func.now())
    )


async def _touch(session: AsyncSession, session_id: uuid.UUID, settings: Settings) -> None:
    """Refresh `last_used_at`, at most once per `TOUCH_INTERVAL_SECONDS`.

    The condition lives in the `WHERE` clause rather than in Python so two
    concurrent requests cannot both decide the row needs updating, and so the
    decision uses the database clock.
    """
    await session.execute(
        update(AuthSession)
        .where(
            AuthSession.id == session_id,
            AuthSession.last_used_at < func.now() - _interval("second", TOUCH_INTERVAL_SECONDS),
        )
        .values(last_used_at=func.now())
    )
    # Committed here rather than with the request's work: a read-only request still
    # needs the touch to persist, and the touch has nothing to do with the handler's
    # transaction.
    await session.commit()


async def _explain_failure(session: AsyncSession, digest: str, settings: Settings) -> Exception:
    """Classify why authentication failed, for a clearer client message.

    Runs only on the failure path, so the happy path stays a single query. The
    comparisons happen in SQL so they use the database clock, like the validity
    check that rejected the session.
    """
    existing = (
        await session.execute(
            select(
                (AuthSession.expires_at <= func.now()).label("expired"),
                (
                    AuthSession.last_used_at
                    < func.now() - _interval("hour", settings.session_idle_ttl_hours)
                ).label("idle"),
                AuthSession.revoked_at,
                User.is_active,
            )
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == digest)
        )
    ).one_or_none()

    if existing is None:
        return InvalidSession()
    if existing.revoked_at is not None or not existing.is_active:
        return InvalidSession()
    if existing.expired or existing.idle:
        return SessionExpired()

    # The session looks valid in isolation, so something else filtered it out.
    return InvalidSession()


async def _roles_for(session: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, Role]:
    rows = await session.execute(
        select(Membership.team_id, Membership.role).where(Membership.user_id == user_id)
    )
    return {row.team_id: parse_role(row.role) for row in rows}


__all__ = [
    "TOUCH_INTERVAL_SECONDS",
    "IssuedSession",
    "TokenPair",
    "authenticate",
    "delete_expired_refresh_tokens",
    "delete_expired_sessions",
    "login",
    "revoke",
    "revoke_all_for_user",
    "revoke_all_refresh_tokens_for_user",
    "revoke_refresh_token",
    "rotate_refresh_token",
]
