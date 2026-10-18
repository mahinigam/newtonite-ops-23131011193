"""Notification reads, acknowledgement, operator counts, and the cross-team "my work" view.

Every endpoint here is self-scoped, so the tests are mostly about *not* leaking: another
person's notification is a 404, another team's work never appears in "my work", and the
operator summary covers only teams the caller administers.
"""

from __future__ import annotations

import uuid

from app.models import Team, User
from app.models.enums import Priority, Role, WorkItemType
from app.worker import process_batch
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import (
    add_member,
    auth_headers,
    create_item_via_api,
    create_team,
    create_user,
    create_work_item,
    login,
    scenario,
    walk_pages,
)

NOTIFICATIONS_URL = "/api/v1/notifications"
MY_ITEMS_URL = "/api/v1/me/items"
OPS_URL = "/api/v1/ops/outbox"


async def _assign_and_deliver(
    client: AsyncClient,
    db: AsyncSession,
    *,
    token: str,
    team_id: uuid.UUID,
    assignee_id: uuid.UUID,
    titles: list[str],
) -> list[str]:
    """Create items assigned to somebody, then run the worker once for the whole batch."""
    ids: list[str] = []
    for title in titles:
        item = await create_item_via_api(
            client, token=token, team_id=team_id, title=title, assignee_id=str(assignee_id)
        )
        ids.append(item["id"])
    await process_batch(db)
    return ids


# --------------------------------------------------------------------------- #
# The notification list
# --------------------------------------------------------------------------- #


async def test_only_my_notifications_are_returned(client: AsyncClient, db: AsyncSession) -> None:
    team, users = await scenario(
        db, roles={"lead": Role.LEAD, "nina": Role.MEMBER, "noah": Role.MEMBER}
    )
    lead = await login(client, users["lead"].email)
    nina = await login(client, users["nina"].email)
    noah = await login(client, users["noah"].email)

    await _assign_and_deliver(
        client, db, token=lead, team_id=team.id, assignee_id=users["nina"].id, titles=["For Nina"]
    )
    await _assign_and_deliver(
        client, db, token=lead, team_id=team.id, assignee_id=users["noah"].id, titles=["For Noah"]
    )

    nina_page = (await client.get(NOTIFICATIONS_URL, headers=auth_headers(nina))).json()
    noah_page = (await client.get(NOTIFICATIONS_URL, headers=auth_headers(noah))).json()

    assert [item["body"] for item in nina_page["items"]] == ["Lead Alpha assigned you “For Nina”."]
    assert [item["body"] for item in noah_page["items"]] == ["Lead Alpha assigned you “For Noah”."]


async def test_the_list_is_paginated_without_duplicates_or_gaps(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Every notification shares one `created_at`, so this proves the `id` tie-breaker.

    A keyset that compared only the timestamp would either loop, skip rows, or repeat the
    last one — the failure mode that only shows up when pages are stitched together.
    """
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    member = await login(client, users["member"].email)
    titles = [f"Batched item {index}" for index in range(5)]
    await _assign_and_deliver(
        client, db, token=lead, team_id=team.id, assignee_id=users["member"].id, titles=titles
    )

    async def fetch(cursor: str | None = None, limit: int | None = None) -> Response:
        params: dict[str, str | int] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return await client.get(NOTIFICATIONS_URL, params=params, headers=auth_headers(member))

    rows = await walk_pages(fetch, limit=2)

    assert len(rows) == 5
    assert len({row["id"] for row in rows}) == 5, "a notification appeared on two pages"


async def test_authentication_is_required(client: AsyncClient, db: AsyncSession) -> None:
    response = await client.get(NOTIFICATIONS_URL)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


# --------------------------------------------------------------------------- #
# Unread state
# --------------------------------------------------------------------------- #


async def test_unread_count_and_acknowledgement(client: AsyncClient, db: AsyncSession) -> None:
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    member = await login(client, users["member"].email)
    await _assign_and_deliver(
        client,
        db,
        token=lead,
        team_id=team.id,
        assignee_id=users["member"].id,
        titles=["First", "Second"],
    )

    async def unread() -> Response:
        return await client.get(f"{NOTIFICATIONS_URL}/unread-count", headers=auth_headers(member))

    assert (await unread()).json() == {"unread": 2}

    listed = (await client.get(NOTIFICATIONS_URL, headers=auth_headers(member))).json()
    first_id = listed["items"][0]["id"]

    marked = await client.post(f"{NOTIFICATIONS_URL}/{first_id}/read", headers=auth_headers(member))
    assert marked.status_code == 204
    assert (await unread()).json() == {"unread": 1}

    all_read = await client.post(f"{NOTIFICATIONS_URL}/read-all", headers=auth_headers(member))
    assert all_read.status_code == 200
    assert all_read.json() == {"marked": 1}
    assert (await unread()).json() == {"unread": 0}

    # Nothing left to change: the count is honest about it.
    again = await client.post(f"{NOTIFICATIONS_URL}/read-all", headers=auth_headers(member))
    assert again.json() == {"marked": 0}


async def test_marking_read_is_idempotent_and_keeps_the_first_timestamp(
    client: AsyncClient, db: AsyncSession
) -> None:
    """`read_at` is when it was first read. A retry must not rewrite it."""
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    member = await login(client, users["member"].email)
    await _assign_and_deliver(
        client, db, token=lead, team_id=team.id, assignee_id=users["member"].id, titles=["Once"]
    )
    notification_id = (await client.get(NOTIFICATIONS_URL, headers=auth_headers(member))).json()[
        "items"
    ][0]["id"]

    assert (
        await client.post(
            f"{NOTIFICATIONS_URL}/{notification_id}/read", headers=auth_headers(member)
        )
    ).status_code == 204
    first_read_at = (
        await db.execute(
            text("SELECT read_at FROM notifications WHERE id = :id"), {"id": notification_id}
        )
    ).scalar_one()

    assert (
        await client.post(
            f"{NOTIFICATIONS_URL}/{notification_id}/read", headers=auth_headers(member)
        )
    ).status_code == 204
    second_read_at = (
        await db.execute(
            text("SELECT read_at FROM notifications WHERE id = :id"), {"id": notification_id}
        )
    ).scalar_one()

    assert second_read_at == first_read_at


async def test_another_persons_notification_is_not_found(
    client: AsyncClient, db: AsyncSession
) -> None:
    """404, not 403: an id that is not yours is indistinguishable from one never issued."""
    team, users = await scenario(
        db, roles={"lead": Role.LEAD, "nina": Role.MEMBER, "noah": Role.MEMBER}
    )
    lead = await login(client, users["lead"].email)
    nina = await login(client, users["nina"].email)
    noah = await login(client, users["noah"].email)
    await _assign_and_deliver(
        client, db, token=lead, team_id=team.id, assignee_id=users["nina"].id, titles=["Nina's"]
    )
    nina_notification = (await client.get(NOTIFICATIONS_URL, headers=auth_headers(nina))).json()[
        "items"
    ][0]["id"]

    response = await client.post(
        f"{NOTIFICATIONS_URL}/{nina_notification}/read", headers=auth_headers(noah)
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    still_unread = (
        await db.execute(
            text("SELECT read_at FROM notifications WHERE id = :id"),
            {"id": nina_notification},
        )
    ).scalar_one()
    assert still_unread is None


# --------------------------------------------------------------------------- #
# Operator visibility
# --------------------------------------------------------------------------- #


async def test_outbox_summary_requires_an_admin(client: AsyncClient, db: AsyncSession) -> None:
    _team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)

    response = await client.get(OPS_URL, headers=auth_headers(lead))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_outbox_summary_covers_only_teams_i_administer(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The caller administers `alpha` and only leads `beta`, so `beta` is not reported."""
    alpha = await create_team(db, key="alpha")
    beta = await create_team(db, key="beta")
    admin = await create_user(db, email="ada.admin@newtonite.dev")
    worker = await create_user(db, email="wes.worker@newtonite.dev")
    await add_member(db, team=alpha, user=admin, role=Role.ADMIN)
    await add_member(db, team=beta, user=admin, role=Role.LEAD)
    await add_member(db, team=alpha, user=worker, role=Role.MEMBER)
    await add_member(db, team=beta, user=worker, role=Role.MEMBER)
    await db.commit()
    token = await login(client, admin.email)

    # Assigned to somebody else: assigning to yourself queues nothing, by design.
    await create_item_via_api(
        client, token=token, team_id=alpha.id, title="Alpha work", assignee_id=str(worker.id)
    )
    await create_item_via_api(
        client, token=token, team_id=beta.id, title="Beta work", assignee_id=str(worker.id)
    )

    body = (await client.get(OPS_URL, headers=auth_headers(token))).json()

    assert [team["team_key"] for team in body["teams"]] == ["alpha"]
    assert body["pending"] == 1
    assert body["teams"][0]["pending"] == 1


async def test_outbox_summary_makes_exhausted_delivery_visible(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A permanently failed event is the one thing an operator must not have to guess at."""
    team, users = await scenario(db, roles={"admin": Role.ADMIN, "member": Role.MEMBER})
    token = await login(client, users["admin"].email)
    await create_item_via_api(
        client,
        token=token,
        team_id=team.id,
        title="Undeliverable",
        assignee_id=str(users["member"].id),
    )

    async def always_fails(session: AsyncSession, event: object) -> bool:
        raise RuntimeError("channel unavailable")

    await process_batch(db, handler=always_fails, max_attempts=1)

    body = (await client.get(OPS_URL, headers=auth_headers(token))).json()

    assert body["failed"] == 1
    assert body["pending"] == 0
    assert body["teams"][0]["failed"] == 1


# --------------------------------------------------------------------------- #
# "My work" across teams
# --------------------------------------------------------------------------- #


async def _multi_team_fixture(
    db: AsyncSession,
) -> tuple[Team, Team, Team, User, User]:
    """One person in two teams, plus an outsider in a third.

    Built by hand rather than with `scenario`, which creates one team at a time; the point
    of this fixture is a single user whose memberships span teams.
    """
    alpha = await create_team(db, key="alpha")
    beta = await create_team(db, key="beta")
    gamma = await create_team(db, key="gamma")
    multi = await create_user(db, email="sam.multi@newtonite.dev", display_name="Sam Multi")
    outsider = await create_user(db, email="olga.outside@newtonite.dev")
    await add_member(db, team=alpha, user=multi, role=Role.MEMBER)
    await add_member(db, team=beta, user=multi, role=Role.MEMBER)
    await add_member(db, team=gamma, user=outsider, role=Role.MEMBER)
    await db.commit()
    return alpha, beta, gamma, multi, outsider


async def test_my_items_spans_my_teams_and_nothing_else(
    client: AsyncClient, db: AsyncSession
) -> None:
    alpha, beta, gamma, multi, outsider = await _multi_team_fixture(db)
    await create_work_item(db, team=alpha, created_by=multi, assignee=multi, title="Alpha assigned")
    await create_work_item(db, team=beta, created_by=multi, assignee=multi, title="Beta assigned")
    await create_work_item(
        db, team=alpha, created_by=multi, assignee=None, title="Alpha unassigned"
    )
    await create_work_item(
        db, team=gamma, created_by=outsider, assignee=outsider, title="Gamma assigned"
    )
    await db.commit()
    token = await login(client, multi.email)

    body = (await client.get(MY_ITEMS_URL, headers=auth_headers(token))).json()

    assert {item["title"] for item in body["items"]} == {"Alpha assigned", "Beta assigned"}
    assert {item["team_id"] for item in body["items"]} == {str(alpha.id), str(beta.id)}


async def test_my_items_applies_filters_in_the_database(
    client: AsyncClient, db: AsyncSession
) -> None:
    alpha, beta, _gamma, multi, _outsider = await _multi_team_fixture(db)
    await create_work_item(
        db,
        team=alpha,
        created_by=multi,
        assignee=multi,
        title="Urgent payment",
        priority=Priority.URGENT,
        item_type=WorkItemType.PAYMENT,
    )
    await create_work_item(
        db, team=beta, created_by=multi, assignee=multi, title="Quiet task", priority=Priority.LOW
    )
    await db.commit()
    token = await login(client, multi.email)

    urgent = (
        await client.get(MY_ITEMS_URL, params={"priority": "urgent"}, headers=auth_headers(token))
    ).json()
    searched = (
        await client.get(MY_ITEMS_URL, params={"q": "quiet"}, headers=auth_headers(token))
    ).json()

    assert [item["title"] for item in urgent["items"]] == ["Urgent payment"]
    assert [item["title"] for item in searched["items"]] == ["Quiet task"]


async def test_my_items_paginate_across_teams_without_duplicates(
    client: AsyncClient, db: AsyncSession
) -> None:
    alpha, beta, _gamma, multi, _outsider = await _multi_team_fixture(db)
    for index in range(6):
        team = alpha if index % 2 == 0 else beta
        await create_work_item(
            db, team=team, created_by=multi, assignee=multi, title=f"Mine {index}"
        )
    await db.commit()
    token = await login(client, multi.email)

    async def fetch(cursor: str | None = None, limit: int | None = None) -> Response:
        params: dict[str, str | int] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        return await client.get(MY_ITEMS_URL, params=params, headers=auth_headers(token))

    rows = await walk_pages(fetch, limit=2)

    assert len(rows) == 6
    assert len({row["id"] for row in rows}) == 6


async def test_my_items_requires_authentication(client: AsyncClient, db: AsyncSession) -> None:
    response = await client.get(MY_ITEMS_URL)
    assert response.status_code == 401


async def test_my_items_carries_the_callers_role_for_each_team(
    client: AsyncClient, db: AsyncSession
) -> None:
    """`available_transitions` must reflect the role in *that* item's team, not one role.

    The same person can be a member in one team and a viewer in another; a single
    role for the whole page would advertise moves the server would then refuse.
    """
    alpha = await create_team(db, key="alpha")
    beta = await create_team(db, key="beta")
    sam = await create_user(db, email="sam.roles@newtonite.dev")
    await add_member(db, team=alpha, user=sam, role=Role.MEMBER)
    await add_member(db, team=beta, user=sam, role=Role.VIEWER)
    await create_work_item(db, team=alpha, created_by=sam, assignee=sam, title="Member team")
    await create_work_item(db, team=beta, created_by=sam, assignee=sam, title="Viewer team")
    await db.commit()
    token = await login(client, sam.email)

    body = (await client.get(MY_ITEMS_URL, headers=auth_headers(token))).json()
    by_title = {item["title"]: item for item in body["items"]}

    # A member can claim and start work; on an already-assigned item, the available move
    # is to start it. A viewer can do nothing.
    assert by_title["Member team"]["available_transitions"] == ["in_progress"]
    assert by_title["Viewer team"]["available_transitions"] == []


async def test_my_items_does_not_accept_an_assignee_parameter(
    client: AsyncClient, db: AsyncSession
) -> None:
    """This endpoint *is* the assigned-to-me view; an assignee would make it a general
    cross-team list, which is a much larger authorization surface."""
    alpha, _beta, _gamma, multi, _outsider = await _multi_team_fixture(db)
    await create_work_item(db, team=alpha, created_by=multi, assignee=multi, title="Mine")
    await db.commit()
    token = await login(client, multi.email)

    # Sending it is simply ignored by FastAPI's parameter binding, and the result is still
    # the caller's own work — never somebody else's.
    body = (
        await client.get(MY_ITEMS_URL, params={"assignee": "none"}, headers=auth_headers(token))
    ).json()

    assert [item["title"] for item in body["items"]] == ["Mine"]
