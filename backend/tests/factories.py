"""Object factories for tests.

Kept separate from `conftest.py` so both fixtures and test modules can import them,
and so arranging a scenario reads as a sentence: create a team, add a member, create
an item assigned to them.

Deliberate shortcuts, with reasons:

* Identifiers are generated in Python rather than by the database default, so a test
  can use an id immediately without a refresh round trip.
* One password hash is computed once and shared. Argon2id is intentionally slow, and
  the suite creates dozens of users; distinct salts matter for real accounts, not for
  fixtures. The password *verification* path is still exercised for real.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models import ActivityEvent, Comment, Membership, Team, User, WorkItem
from app.models.enums import ActivityType, Priority, Role, Status, WorkItemType
from app.security.passwords import hash_password
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TEST_PASSWORD = "test-password-123"
_PASSWORD_HASH = hash_password(TEST_PASSWORD)

DEFAULT_TITLE = "Investigate the failing nightly job"


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    display_name: str | None = None,
    is_active: bool = True,
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email.strip().lower(),
        display_name=display_name or email.split("@")[0].replace(".", " ").title(),
        password_hash=_PASSWORD_HASH,
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    return user


async def create_team(
    session: AsyncSession, *, key: str, name: str | None = None, description: str = ""
) -> Team:
    team = Team(id=uuid.uuid4(), key=key, name=name or key.title(), description=description)
    session.add(team)
    await session.flush()
    return team


async def add_member(session: AsyncSession, *, team: Team, user: User, role: Role) -> Membership:
    membership = Membership(id=uuid.uuid4(), team_id=team.id, user_id=user.id, role=role.value)
    session.add(membership)
    await session.flush()
    return membership


async def create_work_item(
    session: AsyncSession,
    *,
    team: Team,
    created_by: User,
    title: str = DEFAULT_TITLE,
    assignee: User | None = None,
    status: Status = Status.OPEN,
    priority: Priority = Priority.MEDIUM,
    item_type: WorkItemType = WorkItemType.TASK,
    description: str = "",
) -> WorkItem:
    item = WorkItem(
        id=uuid.uuid4(),
        team_id=team.id,
        title=title,
        description=description,
        type=item_type.value,
        priority=priority.value,
        status=status.value,
        assignee_id=assignee.id if assignee else None,
        created_by=created_by.id,
        version=1,
    )
    session.add(item)
    await session.flush()
    return item


async def add_comment(
    session: AsyncSession, *, item: WorkItem, author: User, body: str = "A comment"
) -> Comment:
    comment = Comment(id=uuid.uuid4(), work_item_id=item.id, author_id=author.id, body=body)
    session.add(comment)
    await session.flush()
    return comment


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def idempotency_headers(token: str, key: str | None = None) -> dict[str, str]:
    return auth_headers(token) | {"Idempotency-Key": key or str(uuid.uuid4())}


ITEMS_URL = "/api/v1/teams/{team_id}/items"


async def api_create_item(
    client: AsyncClient,
    *,
    token: str,
    team_id: uuid.UUID,
    key: str | None = None,
    **fields: Any,
) -> Response:
    """POST a create request without asserting the outcome, so a test can."""
    payload: dict[str, Any] = {"title": DEFAULT_TITLE, "type": "task"} | fields
    return await client.post(
        ITEMS_URL.format(team_id=team_id),
        json=payload,
        headers=idempotency_headers(token, key),
    )


async def create_item_via_api(
    client: AsyncClient,
    *,
    token: str,
    team_id: uuid.UUID,
    key: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Create an item through the API and return the body, failing loudly otherwise."""
    response = await api_create_item(client, token=token, team_id=team_id, key=key, **fields)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def etag_headers(token: str, etag: str) -> dict[str, str]:
    """`Authorization` plus the precondition a conditional update requires."""
    return auth_headers(token) | {"If-Match": etag}


async def read_item(client: AsyncClient, *, token: str, item_id: str) -> Response:
    return await client.get(f"/api/v1/items/{item_id}", headers=auth_headers(token))


async def api_claim(client: AsyncClient, *, token: str, item_id: str) -> Response:
    return await client.post(f"/api/v1/items/{item_id}/claim", headers=auth_headers(token))


async def claim_ok(client: AsyncClient, *, token: str, item_id: str) -> dict[str, Any]:
    response = await api_claim(client, token=token, item_id=item_id)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def api_transition(
    client: AsyncClient,
    *,
    token: str,
    item_id: str,
    target: str,
    etag: str | None = None,
    note: str | None = None,
) -> Response:
    """Attempt a transition, sending `etag` if the caller supplied one.

    Omitting `etag` deliberately sends no `If-Match`, which is what the
    precondition tests want to exercise.
    """
    payload: dict[str, Any] = {"to": target}
    if note is not None:
        payload["note"] = note
    headers = etag_headers(token, etag) if etag is not None else auth_headers(token)
    return await client.post(f"/api/v1/items/{item_id}/transitions", json=payload, headers=headers)


async def move_to(
    client: AsyncClient,
    *,
    token: str,
    item_id: str,
    target: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Walk one workflow step the way a real client does.

    Reads the item for its current `ETag`, then sends that as `If-Match`. A test that
    wants a *stale* precondition must call `api_transition` with an explicit `etag`
    instead.
    """
    current = await read_item(client, token=token, item_id=item_id)
    assert current.status_code == 200, current.text
    response = await api_transition(
        client,
        token=token,
        item_id=item_id,
        target=target,
        etag=current.headers["etag"],
        note=note,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def api_patch(
    client: AsyncClient,
    *,
    token: str,
    item_id: str,
    etag: str | None = None,
    **fields: Any,
) -> Response:
    headers = etag_headers(token, etag) if etag is not None else auth_headers(token)
    return await client.patch(f"/api/v1/items/{item_id}", json=fields, headers=headers)


COMMENTS_URL = "/api/v1/items/{item_id}/comments"
ITEM_ACTIVITY_URL = "/api/v1/items/{item_id}/activity"
TEAM_ACTIVITY_URL = "/api/v1/teams/{team_id}/activity"


async def api_comment(
    client: AsyncClient,
    *,
    token: str,
    item_id: str,
    body: str = "Looks like a bad deploy.",
    key: str | None = None,
    omit_key: bool = False,
) -> Response:
    """POST a comment.

    A fresh `Idempotency-Key` is generated unless one is supplied, so the common case is
    retry-safe; `omit_key=True` suppresses the header entirely, which is what the test for
    the missing-key refusal needs.
    """
    headers = auth_headers(token) if omit_key else idempotency_headers(token, key)
    return await client.post(
        COMMENTS_URL.format(item_id=item_id), json={"body": body}, headers=headers
    )


async def comment_ok(
    client: AsyncClient, *, token: str, item_id: str, body: str = "Noted."
) -> dict[str, Any]:
    response = await api_comment(client, token=token, item_id=item_id, body=body)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def read_comments(
    client: AsyncClient,
    *,
    token: str,
    item_id: str,
    cursor: str | None = None,
    limit: int | None = None,
) -> Response:
    params: dict[str, Any] = {}
    if cursor is not None:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = limit
    return await client.get(
        COMMENTS_URL.format(item_id=item_id), params=params, headers=auth_headers(token)
    )


async def read_items(
    client: AsyncClient,
    *,
    token: str,
    team_id: uuid.UUID,
    cursor: str | None = None,
    limit: int | None = None,
) -> Response:
    params: dict[str, Any] = {}
    if cursor is not None:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = limit
    return await client.get(
        ITEMS_URL.format(team_id=team_id), params=params, headers=auth_headers(token)
    )


async def read_item_activity(
    client: AsyncClient,
    *,
    token: str,
    item_id: str,
    cursor: str | None = None,
    limit: int | None = None,
) -> Response:
    params: dict[str, Any] = {}
    if cursor is not None:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = limit
    return await client.get(
        ITEM_ACTIVITY_URL.format(item_id=item_id), params=params, headers=auth_headers(token)
    )


async def read_team_activity(
    client: AsyncClient,
    *,
    token: str,
    team_id: uuid.UUID,
    cursor: str | None = None,
    limit: int | None = None,
    since: str | None = None,
    until: str | None = None,
    actor: str | None = None,
) -> Response:
    params: dict[str, Any] = {}
    if cursor is not None:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = limit
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    if actor is not None:
        params["actor"] = actor
    return await client.get(
        TEAM_ACTIVITY_URL.format(team_id=team_id), params=params, headers=auth_headers(token)
    )


async def walk_pages(
    fetch: Callable[..., Awaitable[Response]], *, limit: int, max_pages: int = 50
) -> list[dict[str, Any]]:
    """Follow `next_cursor` until it runs out, returning every row in order.

    Paging is where a keyset implementation fails quietly — a duplicate or a skipped row
    only shows up when the pages are stitched back together — so tests collect the whole
    walk and assert on it.
    """
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        response = await fetch(cursor=cursor, limit=limit)
        assert response.status_code == 200, response.text
        page = response.json()
        rows.extend(page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            assert cursor is None, "the last page must not offer a cursor"
            break
        assert cursor is not None, "a page with more rows must offer a cursor"
    else:  # pragma: no cover - a walk that never ends is a bug, not a test failure
        raise AssertionError(f"paging did not finish within {max_pages} pages")
    return rows


async def count_comments(session: AsyncSession, item_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM comments WHERE work_item_id = :id"),
                {"id": item_id},
            )
        ).scalar_one()
    )


async def count_events(session: AsyncSession, item_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM activity_events WHERE work_item_id = :id"),
                {"id": item_id},
            )
        ).scalar_one()
    )


async def insert_comments(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    author_id: uuid.UUID,
    bodies: list[str],
    spaced: bool = False,
) -> None:
    """Insert comments directly, in one transaction.

    Used by the pagination tests, which need a *long* list rather than a meaningful one.

    ``spaced=False`` (the default) lets every row share the transaction's `now()`, so the
    order can only be right if the `id` tie-breaker is part of the keyset — the case a
    test that created comments one request at a time would never reach. ``spaced=True``
    gives each row a distinct, increasing timestamp, which is what makes "newest first"
    a deterministic claim rather than a race against the clock.
    """
    base = datetime.now(UTC) - timedelta(days=1)
    for index, body in enumerate(bodies):
        comment = Comment(id=uuid.uuid4(), work_item_id=item_id, author_id=author_id, body=body)
        if spaced:
            comment.created_at = base + timedelta(seconds=index)
        session.add(comment)
    # Committed, not merely flushed: the API reads through its own connection, so rows
    # that only exist in this session's uncommitted transaction would be invisible.
    await session.commit()


async def insert_events(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    team_id: uuid.UUID,
    actor_id: uuid.UUID,
    count: int,
    spaced: bool = False,
) -> None:
    """Insert history rows directly, for the same reason as `insert_comments`.

    The default makes every row share one `created_at`, so reading them back in the right
    order proves the `id` tie-breaker rather than a lucky timestamp.
    """
    base = datetime.now(UTC) - timedelta(days=1)
    for index in range(count):
        event = ActivityEvent(
            work_item_id=item_id,
            team_id=team_id,
            actor_id=actor_id,
            event_type=ActivityType.PRIORITY_CHANGED.value,
            from_value=f"step {index}",
            to_value=f"step {index + 1}",
            meta={},
        )
        if spaced:
            event.created_at = base + timedelta(seconds=index)
        session.add(event)
    # Committed for the same reason as `insert_comments`: the API cannot see an
    # uncommitted transaction on another connection.
    await session.commit()


async def activity_events(session: AsyncSession, item_id: uuid.UUID) -> list[dict[str, Any]]:
    """The item's history, oldest first, with the values each event carries."""
    rows = (
        (
            await session.execute(
                text(
                    "SELECT event_type, from_value, to_value, meta, actor_id "
                    "FROM activity_events "
                    "WHERE work_item_id = :id ORDER BY id"
                ),
                {"id": item_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def raw_item(session: AsyncSession, item_id: uuid.UUID) -> dict[str, Any]:
    """Read the stored row directly, for asserting on what the database holds.

    Deliberately bypasses the API: several tests need to prove that a *rejected*
    request left the persisted state untouched, which an API read could not
    distinguish from an API read of the right value.
    """
    row = (
        (
            await session.execute(
                text(
                    "SELECT title, description, type, priority, status, assignee_id, version "
                    "FROM work_items WHERE id = :id"
                ),
                {"id": item_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def activity_types(session: AsyncSession, item_id: uuid.UUID) -> list[str]:
    """The item's history, oldest first, as a list of event types."""
    rows = (
        (
            await session.execute(
                text("SELECT event_type FROM activity_events WHERE work_item_id = :id ORDER BY id"),
                {"id": item_id},
            )
        )
        .scalars()
        .all()
    )
    return [str(row) for row in rows]


async def count_items(session: AsyncSession, team_id: uuid.UUID | None = None) -> int:
    statement = "SELECT count(*) FROM work_items"
    parameters: dict[str, Any] = {}
    if team_id is not None:
        statement += " WHERE team_id = :team_id"
        parameters["team_id"] = team_id
    return int((await session.execute(text(statement), parameters)).scalar_one())


async def outbox_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Every queued event, oldest first, with the fields the assertions care about."""
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, event_type, aggregate_type, aggregate_id, team_id, payload, "
                    "status, attempts, last_error, processed_at, next_attempt_at "
                    "FROM outbox_events ORDER BY id"
                )
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def count_outbox(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT count(*) FROM outbox_events"))).scalar_one())


async def notification_rows(
    session: AsyncSession, recipient_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """Delivered notifications, oldest first, optionally for one recipient."""
    statement = (
        "SELECT id, outbox_event_id, recipient_id, team_id, work_item_id, event_type, body, "
        "read_at FROM notifications"
    )
    parameters: dict[str, Any] = {}
    if recipient_id is not None:
        statement += " WHERE recipient_id = :recipient_id"
        parameters["recipient_id"] = recipient_id
    statement += " ORDER BY created_at, id"
    rows = (await session.execute(text(statement), parameters)).mappings().all()
    return [dict(row) for row in rows]


async def count_notifications(session: AsyncSession, recipient_id: uuid.UUID | None = None) -> int:
    statement = "SELECT count(*) FROM notifications"
    parameters: dict[str, Any] = {}
    if recipient_id is not None:
        statement += " WHERE recipient_id = :recipient_id"
        parameters["recipient_id"] = recipient_id
    return int((await session.execute(text(statement), parameters)).scalar_one())


async def make_outbox_due(session: AsyncSession) -> None:
    """Skip the backoff wait, so a test can drive several attempts without sleeping.

    Backoff is asserted separately (`test_backoff_grows_then_caps`); this helper is for
    the exhaustion path, which is about what happens *after* the retries, not about the
    waiting.
    """
    await session.execute(
        text("UPDATE outbox_events SET next_attempt_at = now() WHERE status = 'pending'")
    )
    await session.commit()


async def login(client: AsyncClient, email: str, password: str = TEST_PASSWORD) -> str:
    """Log in and return the bearer token, failing loudly if it does not work."""
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


async def scenario(
    session: AsyncSession,
    *,
    roles: dict[str, Role],
    team_key: str = "alpha",
) -> tuple[Team, dict[str, User]]:
    """Create one team and one user per `roles` entry.

    ``roles`` maps an email local part to a role, for example
    ``{"lead": Role.LEAD, "member": Role.MEMBER}``. Returns the team and the users
    keyed by the same names.
    """
    team = await create_team(session, key=team_key)
    users: dict[str, User] = {}
    for name, role in roles.items():
        user = await create_user(session, email=f"{name}.{team_key}@newtonite.dev")
        await add_member(session, team=team, user=user, role=role)
        users[name] = user
    await session.commit()
    return team, users
