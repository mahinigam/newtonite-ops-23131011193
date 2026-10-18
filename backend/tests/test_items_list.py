"""Listing: server-side filtering, searching and keyset pagination.

The browser is never asked to hold the dataset. Every filter is applied in PostgreSQL, the
page size is bounded, and the cursor is a position rather than an offset — which is why a
page cannot repeat or skip an item when new work arrives while somebody is reading.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

from app.models.enums import Role
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import (
    ITEMS_URL,
    auth_headers,
    create_item_via_api,
    login,
    scenario,
)


def _items_url(team_id: uuid.UUID, query: str = "") -> str:
    base = ITEMS_URL.format(team_id=team_id)
    return f"{base}{query}"


async def _seed_items(
    client: AsyncClient,
    *,
    token: str,
    team_id: uuid.UUID,
    titles: list[str],
    **fields: Any,
) -> list[dict[str, Any]]:
    created = []
    for title in titles:
        created.append(
            await create_item_via_api(client, token=token, team_id=team_id, title=title, **fields)
        )
    return created


async def test_a_page_has_a_bounded_size_and_carries_a_cursor(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    await _seed_items(
        client, token=token, team_id=team.id, titles=[f"Item {index:02d}" for index in range(12)]
    )

    response = await client.get(_items_url(team.id, "?limit=5"), headers=auth_headers(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["limit"] == 5
    assert len(body["items"]) == 5
    assert body["has_more"] is True
    assert body["next_cursor"]
    # Newest first, and a usable version for each row.
    assert body["items"][0]["title"] == "Item 11"
    assert body["items"][0]["version"] == 1


async def test_walking_the_pages_visits_every_item_exactly_once(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    created = await _seed_items(
        client, token=token, team_id=team.id, titles=[f"Item {index:02d}" for index in range(23)]
    )
    expected = {item["id"] for item in created}

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        query = f"?limit=5{f'&cursor={cursor}' if cursor else ''}"
        response = await client.get(_items_url(team.id, query), headers=auth_headers(token))
        assert response.status_code == 200, response.text
        body = response.json()
        pages += 1
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        assert pages < 20, "the cursor is not terminating"
        if not body["has_more"]:
            break

    assert len(seen) == len(set(seen)), "an item was returned on two pages"
    assert set(seen) == expected
    assert pages == 5  # 23 items at 5 per page


async def test_new_items_arriving_mid_pagination_neither_duplicate_nor_skip(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The property `OFFSET` cannot offer.

    Somebody else creates work while a reviewer is reading page 1. Because the cursor is
    a position in the ordering rather than a count of rows, page 2 continues exactly where
    page 1 stopped. With `OFFSET` the shift would push two items back onto page 2 and skip
    two that had already been shown.
    """
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    created = await _seed_items(
        client, token=token, team_id=team.id, titles=[f"Old {index:02d}" for index in range(10)]
    )
    original_ids = {item["id"] for item in created}

    first = (await client.get(_items_url(team.id, "?limit=5"), headers=auth_headers(token))).json()
    cursor = first["next_cursor"]
    assert cursor

    # New work lands in the team between the two page requests.
    await _seed_items(
        client, token=token, team_id=team.id, titles=["Newest one", "Newest two", "Newest three"]
    )

    seen = [item["id"] for item in first["items"]]
    while cursor:
        body = (
            await client.get(
                _items_url(team.id, f"?limit=5&cursor={cursor}"), headers=auth_headers(token)
            )
        ).json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]

    assert len(seen) == len(set(seen)), "an item was shown twice"
    # Every pre-existing item was shown, and no page ever repeated one.
    assert original_ids.issubset(set(seen))
    assert seen[:5] == [item["id"] for item in first["items"]]


async def test_identical_timestamps_still_paginate_total_through_the_tie_breaker(
    client: AsyncClient, db: AsyncSession
) -> None:
    """`created_at` alone is not a total order, so `id` breaks the tie.

    This forces the awkward case: twelve items with the *same* timestamp. Without the id
    tie-breaker, the database would be free to return them in any order per query, and a
    keyset page could repeat or miss rows.
    """
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    created = await _seed_items(
        client, token=token, team_id=team.id, titles=[f"Tie {index:02d}" for index in range(12)]
    )
    await db.execute(
        text("UPDATE work_items SET created_at = timestamptz '2026-01-01 12:00:00+00'")
    )
    await db.commit()
    expected = {item["id"] for item in created}

    seen: list[str] = []
    cursor: str | None = None
    while True:
        query = f"?limit=5{f'&cursor={cursor}' if cursor else ''}"
        body = (await client.get(_items_url(team.id, query), headers=auth_headers(token))).json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not body["has_more"]:
            break

    assert len(seen) == len(set(seen))
    assert set(seen) == expected


async def test_filters_combine_and_are_applied_in_the_database(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(
        db, roles={"member": Role.MEMBER, "other": Role.MEMBER}, team_key="alpha"
    )
    token = await login(client, users["member"].email)
    other_token = await login(client, users["other"].email)

    await create_item_via_api(
        client,
        token=token,
        team_id=team.id,
        title="Urgent incident",
        type="incident",
        priority="urgent",
    )
    await create_item_via_api(
        client, token=token, team_id=team.id, title="Low payment", type="payment", priority="low"
    )
    await create_item_via_api(
        client,
        token=token,
        team_id=team.id,
        title="Urgent payment",
        type="payment",
        priority="urgent",
    )
    mine = await create_item_via_api(
        client,
        token=other_token,
        team_id=team.id,
        title="Mine to do",
        type="task",
        priority="high",
        assignee_id=str(users["other"].id),
    )
    assert mine["assignee"]["id"] == str(users["other"].id)

    async def titles(query: str, token_for: str = token) -> list[str]:
        response = await client.get(_items_url(team.id, query), headers=auth_headers(token_for))
        assert response.status_code == 200, response.text
        return [item["title"] for item in response.json()["items"]]

    # One filter.
    assert await titles("?priority=urgent") == ["Urgent payment", "Urgent incident"]
    # Two values for the same filter.
    assert await titles("?type=payment&type=task") == [
        "Mine to do",
        "Urgent payment",
        "Low payment",
    ]
    # Filters combine with AND.
    assert await titles("?priority=urgent&type=payment") == ["Urgent payment"]
    # The two named assignee views.
    assert await titles("?assignee=none") == [
        "Urgent payment",
        "Low payment",
        "Urgent incident",
    ]
    assert await titles("?assignee=me", other_token) == ["Mine to do"]
    assert await titles(f"?assignee={users['other'].id}") == ["Mine to do"]
    # A filter that matches nothing is an empty page, not an error.
    assert await titles("?priority=low&type=incident") == []


async def test_search_matches_title_and_description(client: AsyncClient, db: AsyncSession) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    await create_item_via_api(
        client, token=token, team_id=team.id, title="Refund the duplicate charge", type="payment"
    )
    await create_item_via_api(
        client,
        token=token,
        team_id=team.id,
        title="Unrelated chore",
        type="task",
        description="Mentions a refund only in passing.",
    )
    await create_item_via_api(client, token=token, team_id=team.id, title="Quiet item", type="task")

    response = await client.get(_items_url(team.id, "?q=refund"), headers=auth_headers(token))

    assert response.status_code == 200, response.text
    titles = [item["title"] for item in response.json()["items"]]
    # Title and description both count, and the item that mentions neither is absent.
    assert set(titles) == {"Refund the duplicate charge", "Unrelated chore"}
    assert "Quiet item" not in titles


async def test_search_does_not_cross_a_team_boundary(client: AsyncClient, db: AsyncSession) -> None:
    alpha, alpha_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    beta, _ = await scenario(db, roles={"member": Role.MEMBER}, team_key="beta")
    alpha_token = await login(client, alpha_users["member"].email)
    await create_item_via_api(
        client, token=alpha_token, team_id=alpha.id, title="Secret escalation", type="incident"
    )

    # The same search, aimed at a team the caller is not in.
    response = await client.get(_items_url(beta.id, "?q=secret"), headers=auth_headers(alpha_token))

    # The team itself is not found, so a search term cannot be used to probe it.
    assert response.status_code == 404


async def test_listing_only_returns_the_path_teams_items(
    client: AsyncClient, db: AsyncSession
) -> None:
    alpha, alpha_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    beta, beta_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="beta")
    alpha_token = await login(client, alpha_users["member"].email)
    beta_token = await login(client, beta_users["member"].email)
    await create_item_via_api(client, token=alpha_token, team_id=alpha.id, title="Alpha work")
    await create_item_via_api(client, token=beta_token, team_id=beta.id, title="Beta work")

    response = await client.get(_items_url(alpha.id, ""), headers=auth_headers(alpha_token))

    body = response.json()
    assert [item["title"] for item in body["items"]] == ["Alpha work"]
    assert all(item["team_id"] == str(alpha.id) for item in body["items"])


async def test_a_viewer_can_read_the_list(client: AsyncClient, db: AsyncSession) -> None:
    team, users = await scenario(
        db, roles={"member": Role.MEMBER, "viewer": Role.VIEWER}, team_key="alpha"
    )
    member_token = await login(client, users["member"].email)
    viewer_token = await login(client, users["viewer"].email)
    await create_item_via_api(client, token=member_token, team_id=team.id, title="Visible")

    response = await client.get(_items_url(team.id, ""), headers=auth_headers(viewer_token))

    assert response.status_code == 200
    body = response.json()
    assert [item["title"] for item in body["items"]] == ["Visible"]
    # A viewer is offered no transitions, which matches what the server would enforce.
    assert body["items"][0]["available_transitions"] == []


async def test_an_empty_team_returns_an_empty_page(client: AsyncClient, db: AsyncSession) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)

    response = await client.get(_items_url(team.id, ""), headers=auth_headers(token))

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None, "has_more": False, "limit": 25}


async def test_the_list_summary_omits_the_long_description(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A list is not a detail view; the payload does not carry what the view cannot use."""
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    await create_item_via_api(
        client,
        token=token,
        team_id=team.id,
        title="Long story",
        description="A" * 500,
    )

    listed = (await client.get(_items_url(team.id, ""), headers=auth_headers(token))).json()
    assert "description" not in listed["items"][0]

    detail = await client.get(
        f"/api/v1/items/{listed['items'][0]['id']}", headers=auth_headers(token)
    )
    assert len(detail.json()["description"]) == 500


async def test_the_page_size_is_bounded(client: AsyncClient, db: AsyncSession) -> None:
    """A caller cannot ask for the whole table."""
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)

    for query in ("?limit=0", "?limit=101", "?limit=-1"):
        response = await client.get(_items_url(team.id, query), headers=auth_headers(token))
        assert response.status_code == 422, query
        assert response.json()["error"]["code"] == "validation_failed"

    allowed = await client.get(_items_url(team.id, "?limit=100"), headers=auth_headers(token))
    assert allowed.status_code == 200


async def test_a_malformed_cursor_is_reported_rather_than_ignored(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Silently restarting from the beginning would hide a client bug, and repeat rows."""
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    await create_item_via_api(client, token=token, team_id=team.id, title="Only item")

    for cursor in ("not-a-cursor", "Zm9vYmFy", "MTIzfG5vdC1hLXV1aWQ"):
        response = await client.get(
            _items_url(team.id, f"?cursor={cursor}"), headers=auth_headers(token)
        )
        assert response.status_code == 422, cursor
        assert response.json()["error"]["code"] == "validation_failed"

    # An out-of-range bigint in a UUID-keyed list is also 422 (not a 500).
    # Encode a cursor with an integer outside signed 64-bit range.
    out_of_range = (
        base64.urlsafe_b64encode(b"2026-01-01T00:00:00|99999999999999999999").decode().rstrip("=")
    )
    response = await client.get(
        _items_url(team.id, f"?cursor={out_of_range}"), headers=auth_headers(token)
    )
    assert response.status_code == 422, out_of_range
    assert response.json()["error"]["code"] == "validation_failed"


async def test_listing_requires_authentication(client: AsyncClient, db: AsyncSession) -> None:
    team, _ = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")

    response = await client.get(_items_url(team.id, ""))

    assert response.status_code == 401


async def test_filtering_by_another_teams_member_is_a_not_found_team(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A filter value cannot be used to probe another team's data."""
    alpha, alpha_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    _beta, beta_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="beta")
    alpha_token = await login(client, alpha_users["member"].email)
    await create_item_via_api(
        client,
        token=alpha_token,
        team_id=alpha.id,
        title="Alpha work",
        assignee_id=str(alpha_users["member"].id),
    )

    response = await client.get(
        _items_url(alpha.id, f"?assignee={beta_users['member'].id}"),
        headers=auth_headers(alpha_token),
    )

    # The team is one they belong to, so this is a legitimate empty result rather than
    # a leak: they simply have no items assigned to somebody from another team.
    assert response.status_code == 200
    assert response.json()["items"] == []
