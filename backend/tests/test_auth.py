"""Authentication: login, session lifetime, revocation and token storage.

These tests treat the token as an opaque credential and check the properties that
matter: it cannot be enumerated, it cannot be recovered from the database, it stops
working when it should, and a failure never says more than it has to.
"""

from __future__ import annotations

import hashlib

from app.models.enums import Role
from app.security.tokens import TOKEN_LENGTH, hash_token
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import auth_headers, create_user, login, scenario

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"


async def test_login_returns_a_token_and_the_user(client: AsyncClient, db: AsyncSession) -> None:
    await create_user(db, email="nina@newtonite.dev", display_name="Nina Petrova")
    await db.commit()

    response = await client.post(
        LOGIN, json={"email": "nina@newtonite.dev", "password": "test-password-123"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token_type"] == "Bearer"
    assert len(body["access_token"]) == TOKEN_LENGTH
    assert body["user"]["email"] == "nina@newtonite.dev"
    # The response describes the user; it must not describe their credentials.
    assert "password_hash" not in body["user"]
    assert "password" not in body


async def test_login_normalises_case_and_whitespace(client: AsyncClient, db: AsyncSession) -> None:
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()

    response = await client.post(
        LOGIN, json={"email": "  NINA@Newtonite.DEV  ", "password": "test-password-123"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["user"]["email"] == "nina@newtonite.dev"


async def test_unknown_email_and_wrong_password_are_indistinguishable(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The login response must not reveal whether an account exists.

    Both cases return the same status and an identical body, so an attacker cannot
    use the endpoint to enumerate valid addresses.
    """
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()

    unknown = await client.post(
        LOGIN, json={"email": "nobody@newtonite.dev", "password": "test-password-123"}
    )
    wrong_password = await client.post(
        LOGIN, json={"email": "nina@newtonite.dev", "password": "not-the-password"}
    )

    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json() == wrong_password.json()
    assert unknown.json()["error"]["code"] == "invalid_credentials"


async def test_inactive_account_cannot_sign_in(client: AsyncClient, db: AsyncSession) -> None:
    await create_user(db, email="gone@newtonite.dev", is_active=False)
    await db.commit()

    response = await client.post(
        LOGIN, json={"email": "gone@newtonite.dev", "password": "test-password-123"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_only_the_token_digest_is_stored(client: AsyncClient, db: AsyncSession) -> None:
    """A database disclosure must not hand an attacker usable sessions."""
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()
    token = await login(client, "nina@newtonite.dev")

    stored = (await db.execute(text("SELECT token_hash FROM auth_sessions"))).scalars().all()

    assert stored == [hash_token(token)]
    assert stored == [hashlib.sha256(token.encode()).hexdigest()]
    assert token not in stored
    # And the raw token appears in no column of the table.
    row = (await db.execute(text("SELECT * FROM auth_sessions"))).mappings().one()
    assert token not in {str(value) for value in row.values()}


async def test_two_logins_produce_independent_sessions(
    client: AsyncClient, db: AsyncSession
) -> None:
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()

    first = await login(client, "nina@newtonite.dev")
    second = await login(client, "nina@newtonite.dev")

    assert first != second
    for token in (first, second):
        assert (await client.get(ME, headers=auth_headers(token))).status_code == 200

    # Signing out of one leaves the other alone.
    assert (await client.post(LOGOUT, headers=auth_headers(first))).status_code == 200
    assert (await client.get(ME, headers=auth_headers(first))).status_code == 401
    assert (await client.get(ME, headers=auth_headers(second))).status_code == 200


async def test_missing_or_malformed_authorization_header_is_401(client: AsyncClient) -> None:
    for headers in (
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic bmluYTpwdw=="},
        {"Authorization": "Token abc123"},
    ):
        response = await client.get(ME, headers=headers)
        assert response.status_code == 401, headers
        assert response.json()["error"]["code"] == "not_authenticated"
        # Correct HTTP semantics for a bearer-protected resource.
        assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_unknown_token_is_rejected_as_invalid_session(
    client: AsyncClient, db: AsyncSession
) -> None:
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()

    response = await client.get(ME, headers=auth_headers("x" * TOKEN_LENGTH))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_session"


async def test_logout_revokes_the_session(client: AsyncClient, db: AsyncSession) -> None:
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()
    token = await login(client, "nina@newtonite.dev")

    logout = await client.post(LOGOUT, headers=auth_headers(token))

    assert logout.status_code == 200
    assert logout.json() == {"revoked": True}
    assert (await client.get(ME, headers=auth_headers(token))).status_code == 401
    # The session row survives, marked revoked, rather than being deleted.
    revoked_at = (await db.execute(text("SELECT revoked_at FROM auth_sessions"))).scalar_one()
    assert revoked_at is not None


async def test_absolute_expiry_is_enforced(client: AsyncClient, db: AsyncSession) -> None:
    """The session dies after its fixed lifetime even if it is in active use."""
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()
    token = await login(client, "nina@newtonite.dev")

    await db.execute(text("UPDATE auth_sessions SET expires_at = now() - interval '1 minute'"))
    await db.commit()

    response = await client.get(ME, headers=auth_headers(token))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


async def test_idle_expiry_is_enforced(client: AsyncClient, db: AsyncSession) -> None:
    """An unused session dies well before its absolute lifetime."""
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()
    token = await login(client, "nina@newtonite.dev")

    # The default idle window is 2 hours; the absolute lifetime is 12.
    await db.execute(text("UPDATE auth_sessions SET last_used_at = now() - interval '3 hours'"))
    await db.commit()

    response = await client.get(ME, headers=auth_headers(token))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


async def test_activity_refreshes_the_idle_window(client: AsyncClient, db: AsyncSession) -> None:
    """Use keeps a session alive, which is what "idle" means."""
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()
    token = await login(client, "nina@newtonite.dev")

    # Older than the touch interval, so the next request should refresh it, but
    # still inside the idle window.
    await db.execute(text("UPDATE auth_sessions SET last_used_at = now() - interval '10 minutes'"))
    await db.commit()

    assert (await client.get(ME, headers=auth_headers(token))).status_code == 200

    last_used_at = (
        await db.execute(
            text("SELECT last_used_at > now() - interval '1 minute' FROM auth_sessions")
        )
    ).scalar_one()
    assert last_used_at is True


async def test_expired_session_is_not_resurrected_by_signing_in_again(
    client: AsyncClient, db: AsyncSession
) -> None:
    await create_user(db, email="nina@newtonite.dev")
    await db.commit()
    stale = await login(client, "nina@newtonite.dev")

    await db.execute(text("UPDATE auth_sessions SET expires_at = now() - interval '1 hour'"))
    await db.commit()

    fresh = await login(client, "nina@newtonite.dev")

    assert (await client.get(ME, headers=auth_headers(stale))).status_code == 401
    assert (await client.get(ME, headers=auth_headers(fresh))).status_code == 200


async def test_me_lists_teams_with_the_callers_role(client: AsyncClient, db: AsyncSession) -> None:
    _alpha, alpha_users = await scenario(
        db, roles={"lead": Role.LEAD, "member": Role.MEMBER}, team_key="alpha"
    )
    beta, _ = await scenario(db, roles={"multi": Role.VIEWER}, team_key="beta")
    # The same account in two teams with different roles.
    await db.execute(
        text(
            "INSERT INTO memberships (id, user_id, team_id, role) "
            "VALUES (gen_random_uuid(), :user, :team, 'viewer')"
        ),
        {"user": alpha_users["member"].id, "team": beta.id},
    )
    await db.commit()

    token = await login(client, alpha_users["member"].email)
    response = await client.get(ME, headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["display_name"]
    roles = {m["team_key"]: m["role"] for m in body["memberships"]}
    assert roles == {"alpha": "member", "beta": "viewer"}
    assert set(body["user"]) == {"id", "email", "display_name"}


async def test_me_requires_authentication(client: AsyncClient, db: AsyncSession) -> None:
    response = await client.get(ME)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
