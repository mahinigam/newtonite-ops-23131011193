"""Reading activity history: two views, two cursors, one append-only table.

History is written by the operations it describes (and `test_activity_atomicity.py` proves
it cannot be written without them). This file is about *reading* it:

* the item timeline is newest first, carries the stored values, and names the actor;
* the team feed covers every item in one team and no item from another;
* a viewer may read; a non-member is told the item does not exist;
* paging is keyset, so a long timeline is walked without duplicates or gaps — including
  when every row shares a timestamp and only the `id` tie-breaker separates them;
* the activity cursor is the activity list's own, and paging items in between does not
  disturb it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.enums import Role
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import (
    ITEM_ACTIVITY_URL,
    TEAM_ACTIVITY_URL,
    api_patch,
    comment_ok,
    count_events,
    create_item_via_api,
    insert_events,
    login,
    read_item,
    read_item_activity,
    read_items,
    read_team_activity,
    scenario,
    walk_pages,
)


async def test_the_item_timeline_is_newest_first_and_carries_the_values(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    item = await create_item_via_api(client, token=token, team_id=team.id, title="A short life")
    item_id = item["id"]

    current = await read_item(client, token=token, item_id=item_id)
    patched = await api_patch(
        client, token=token, item_id=item_id, etag=current.headers["etag"], priority="urgent"
    )
    assert patched.status_code == 200, patched.text
    await client.post(
        f"/api/v1/items/{item_id}/claim",
        headers={"Authorization": f"Bearer {token}"},
    )
    await comment_ok(client, token=token, item_id=item_id, body="Taking a look")

    response = await read_item_activity(client, token=token, item_id=item_id)

    assert response.status_code == 200, response.text
    events = response.json()["items"]
    # Newest first, and one event per meaningful change, in the order they happened.
    assert [event["event_type"] for event in events] == [
        "commented",
        "claimed",
        "priority_changed",
        "created",
    ]
    assert all(event["work_item_id"] == item_id for event in events)
    assert all(event["actor"]["email"] == users["member"].email for event in events)

    by_type = {event["event_type"]: event for event in events}
    assert by_type["priority_changed"]["from_value"] == "medium"
    assert by_type["priority_changed"]["to_value"] == "urgent"
    assert by_type["claimed"]["to_value"] == str(users["member"].id)
    assert by_type["created"]["from_value"] is None
    # A comment carries no field change, only a reference to the comment.
    assert by_type["commented"]["meta"]["comment_id"]


async def test_the_timeline_pages_to_the_end(client: AsyncClient, db: AsyncSession) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    item = await create_item_via_api(client, token=token, team_id=team.id, title="Busy")
    await insert_events(
        db,
        item_id=uuid.UUID(item["id"]),
        team_id=team.id,
        actor_id=users["member"].id,
        count=20,
        spaced=True,
    )

    rows = await walk_pages(
        lambda **kw: read_item_activity(client, token=token, item_id=item["id"], **kw), limit=6
    )

    # 20 inserted events plus the `created` one, each exactly once, newest first.
    assert len(rows) == 21
    assert [row["to_value"] for row in rows[1:]] == [
        f"step {index + 1}" for index in range(19, -1, -1)
    ]


async def test_the_order_stays_total_when_every_event_shares_a_timestamp(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The `id` tie-breaker, on the timeline.

    One transaction writes 30 events that all share `created_at`. Without `id` in the
    keyset the pages would repeat and skip rows; with it, the walk is exact.
    """
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    item = await create_item_via_api(client, token=token, team_id=team.id, title="Same instant")
    await insert_events(
        db,
        item_id=uuid.UUID(item["id"]),
        team_id=team.id,
        actor_id=users["member"].id,
        count=30,
    )
    assert await count_events(db, uuid.UUID(item["id"])) == 31

    rows = await walk_pages(
        lambda **kw: read_item_activity(client, token=token, item_id=item["id"], **kw), limit=7
    )

    assert len(rows) == 31
    assert len({row["id"] for row in rows}) == 31, "a row was returned twice or skipped"
    keys = [(row["created_at"], row["id"]) for row in rows]
    assert len({key[0] for key in keys}) > 1, "the created event is newer than the rest"
    assert keys == sorted(keys, reverse=True), "the page order is not the declared order"


async def test_a_viewer_can_read_the_timeline(client: AsyncClient, db: AsyncSession) -> None:
    """Viewing history is reading, and a viewer's role is to read."""
    team, users = await scenario(
        db, roles={"member": Role.MEMBER, "viewer": Role.VIEWER}, team_key="alpha"
    )
    member_token = await login(client, users["member"].email)
    viewer_token = await login(client, users["viewer"].email)
    item = await create_item_via_api(
        client, token=member_token, team_id=team.id, title="On the record"
    )

    response = await read_item_activity(client, token=viewer_token, item_id=item["id"])

    assert response.status_code == 200, response.text
    assert [event["event_type"] for event in response.json()["items"]] == ["created"]


async def test_a_non_member_cannot_read_an_item_timeline(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A timeline can name people and describe changes, so it is team-only.

    The refusal is a `404` that is byte-identical to the one for an invented id, so the
    response cannot be used to discover which item ids exist.
    """
    alpha, alpha_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    _beta, beta_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="beta")
    alpha_token = await login(client, alpha_users["member"].email)
    beta_token = await login(client, beta_users["member"].email)
    item = await create_item_via_api(
        client, token=alpha_token, team_id=alpha.id, title="Alpha only"
    )

    foreign = await read_item_activity(client, token=beta_token, item_id=item["id"])
    unknown = await read_item_activity(client, token=beta_token, item_id=str(uuid.uuid4()))

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


async def test_the_team_feed_covers_the_whole_team_newest_first(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    first = await create_item_via_api(client, token=token, team_id=team.id, title="First")
    second = await create_item_via_api(client, token=token, team_id=team.id, title="Second")

    response = await read_team_activity(client, token=token, team_id=team.id)

    assert response.status_code == 200, response.text
    events = response.json()["items"]
    assert {event["work_item_id"] for event in events} == {first["id"], second["id"]}
    # The most recent creation is the most recent event.
    assert events[0]["work_item_id"] == second["id"]
    assert events[0]["event_type"] == "created"
    keys = [(event["created_at"], event["id"]) for event in events]
    assert keys == sorted(keys, reverse=True)


async def test_the_team_feed_never_includes_another_team(
    client: AsyncClient, db: AsyncSession
) -> None:
    alpha, alpha_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    beta, beta_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="beta")
    alpha_token = await login(client, alpha_users["member"].email)
    beta_token = await login(client, beta_users["member"].email)
    mine = await create_item_via_api(
        client, token=alpha_token, team_id=alpha.id, title="Alpha work"
    )
    theirs = await create_item_via_api(client, token=beta_token, team_id=beta.id, title="Beta work")

    alpha_feed = await read_team_activity(client, token=alpha_token, team_id=alpha.id)
    beta_feed = await read_team_activity(client, token=beta_token, team_id=beta.id)

    alpha_ids = {event["work_item_id"] for event in alpha_feed.json()["items"]}
    beta_ids = {event["work_item_id"] for event in beta_feed.json()["items"]}
    assert alpha_ids == {mine["id"]}
    assert beta_ids == {theirs["id"]}
    assert not alpha_ids & beta_ids


async def test_a_non_member_cannot_read_the_team_feed(
    client: AsyncClient, db: AsyncSession
) -> None:
    alpha, _alpha_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    _beta, beta_users = await scenario(db, roles={"member": Role.MEMBER}, team_key="beta")
    beta_token = await login(client, beta_users["member"].email)

    foreign = await read_team_activity(client, token=beta_token, team_id=alpha.id)
    unknown = await read_team_activity(client, token=beta_token, team_id=uuid.uuid4())

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    assert foreign.json()["error"]["code"] == "not_found"


async def test_the_activity_cursor_survives_paging_something_else_in_between(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Activity and items paginate on their own cursors.

    A client reading a timeline and a list at the same time is normal; the two must not
    share a position. This fetches one page of activity, pages the item list with *its*
    cursor, then resumes the timeline from the cursor it was given — and the resumed page
    must continue exactly where the first one stopped.
    """
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    items = [
        await create_item_via_api(client, token=token, team_id=team.id, title=f"Item {index}")
        for index in range(5)
    ]

    first = await read_team_activity(client, token=token, team_id=team.id, limit=2)
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["has_more"] is True
    cursor = page["next_cursor"]
    assert cursor

    # A completely different list, paged with its own cursor, in between.
    listed = await read_items(client, token=token, team_id=team.id, limit=3)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) == 3

    second = await read_team_activity(client, token=token, team_id=team.id, cursor=cursor, limit=2)
    assert second.status_code == 200, second.text

    seen = [event["id"] for event in page["items"]]
    resumed = [event["id"] for event in second.json()["items"]]
    assert not set(seen) & set(resumed), "the resumed page repeated a row"
    assert seen == sorted(seen, reverse=True) and resumed == sorted(resumed, reverse=True)

    # And the whole thing still walks cleanly from the start.
    rows = await walk_pages(
        lambda **kw: read_team_activity(client, token=token, team_id=team.id, **kw), limit=2
    )
    assert len(rows) == len(items)  # one `created` event per item
    assert len({row["id"] for row in rows}) == len(items)


async def test_a_cursor_from_another_list_is_refused(client: AsyncClient, db: AsyncSession) -> None:
    """Cursors belong to one list.

    An item list's cursor names a UUID position; the activity timeline's names an integer
    one, because `activity_events` is keyed by a bigint identity. The two cannot be
    swapped, so "activity paginates independently of item lists" is enforced rather than
    merely intended.
    """
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    for index in range(3):
        await create_item_via_api(client, token=token, team_id=team.id, title=f"Item {index}")

    listed = await read_items(client, token=token, team_id=team.id, limit=1)
    items_cursor = listed.json()["next_cursor"]
    assert items_cursor

    response = await read_team_activity(client, token=token, team_id=team.id, cursor=items_cursor)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_activity_is_read_only_over_http(client: AsyncClient, db: AsyncSession) -> None:
    """History is produced by the operations it describes, never by a client."""
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    item = await create_item_via_api(client, token=token, team_id=team.id, title="Immutable")
    headers = {"Authorization": f"Bearer {token}"}
    item_url = ITEM_ACTIVITY_URL.format(item_id=item["id"])
    team_url = TEAM_ACTIVITY_URL.format(team_id=team.id)

    attempts = [
        await client.post(item_url, json={"event_type": "created"}, headers=headers),
        await client.patch(item_url, json={"to_value": "closed"}, headers=headers),
        await client.delete(item_url, headers=headers),
        await client.put(team_url, json={}, headers=headers),
    ]

    for attempt in attempts:
        assert attempt.status_code == 405, (attempt.request.method, attempt.text)
        assert attempt.json()["error"]["code"] == "method_not_allowed"


async def test_a_bad_cursor_or_limit_is_a_validation_error(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    item = await create_item_via_api(client, token=token, team_id=team.id, title="Bounded")

    for limit in (0, 101):
        response = await read_item_activity(client, token=token, item_id=item["id"], limit=limit)
        assert response.status_code == 422, limit

    garbage = await read_item_activity(
        client, token=token, item_id=item["id"], cursor="not-a-cursor"
    )
    assert garbage.status_code == 422
    assert garbage.json()["error"]["code"] == "validation_failed"


async def test_reading_activity_requires_authentication(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    item = await create_item_via_api(client, token=token, team_id=team.id, title="Closed door")

    response = await client.get(ITEM_ACTIVITY_URL.format(item_id=item["id"]))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_the_team_feed_names_the_item_but_the_timeline_does_not(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A feed spanning many items has to say *which* item.

    "Lena Fischer assigned this item" is unambiguous on that item's page and useless in a
    team feed. The title is joined for the feed; the item timeline keeps its shape and does
    not pay for a join it does not need.
    """
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    item = await create_item_via_api(
        client, token=token, team_id=team.id, title="Refund the duplicate charge"
    )

    feed = await read_team_activity(client, token=token, team_id=team.id)
    timeline = await read_item_activity(client, token=token, item_id=item["id"])

    assert feed.status_code == 200, feed.text
    assert feed.json()["items"][0]["work_item_title"] == "Refund the duplicate charge"
    assert timeline.status_code == 200, timeline.text
    assert timeline.json()["items"][0]["work_item_title"] is None


async def test_the_feed_shows_the_current_title_not_a_snapshot(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Names are joined live, because a stale title makes the feed harder to use.

    Notifications snapshot their body — they are a message that was sent. History is a
    view of the item as it is now, so a rename must show through it.
    """
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    item = await create_item_via_api(client, token=token, team_id=team.id, title="Before")
    current = await read_item(client, token=token, item_id=item["id"])
    renamed = await api_patch(
        client, token=token, item_id=item["id"], etag=current.headers["etag"], title="After"
    )
    assert renamed.status_code == 200, renamed.text

    feed = await read_team_activity(client, token=token, team_id=team.id)

    created = [event for event in feed.json()["items"] if event["event_type"] == "created"]
    assert [event["work_item_title"] for event in created] == ["After"]


async def test_the_feed_can_be_bounded_by_an_inclusive_date_range(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Both bounds include the row that sits exactly on them.

    A "From 17 Oct" control that silently dropped the 17th at 00:00 would be wrong, and a
    "To 18 Oct" control that excluded the 18th would be worse. The boundary is asserted
    from the stored timestamp, not from a rounded one.
    """
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    item = await create_item_via_api(client, token=token, team_id=team.id, title="Bounded")
    await insert_events(
        db,
        item_id=uuid.UUID(item["id"]),
        team_id=team.id,
        actor_id=users["member"].id,
        count=5,
        spaced=True,
    )
    everything = await read_team_activity(client, token=token, team_id=team.id)
    assert everything.status_code == 200, everything.text
    steps = [
        event for event in everything.json()["items"] if event["event_type"] == "priority_changed"
    ]
    assert len(steps) == 5
    middle = sorted(steps, key=lambda event: event["created_at"])[2]
    boundary = middle["created_at"]

    since_page = await read_team_activity(
        client, token=token, team_id=team.id, since=boundary, limit=100
    )
    until_page = await read_team_activity(
        client, token=token, team_id=team.id, until=boundary, limit=100
    )

    assert since_page.status_code == 200, since_page.text
    assert until_page.status_code == 200, until_page.text
    since_ids = {event["id"] for event in since_page.json()["items"]}
    until_ids = {event["id"] for event in until_page.json()["items"]}
    # The row on the boundary belongs to both windows: inclusive at each end.
    assert middle["id"] in since_ids and middle["id"] in until_ids
    # And the inserted steps split cleanly around it: 3 on the older side, 3 newer.
    inserted = {event["id"] for event in steps}
    assert len(until_ids & inserted) == 3
    assert len(since_ids & inserted) == 3
    # The windows meet exactly on the boundary row, and between them cover the whole feed.
    assert since_ids & until_ids == {middle["id"]}
    assert since_ids | until_ids == {event["id"] for event in everything.json()["items"]}

    # Paging a range walks that range and stops at its edge.
    ranged = await walk_pages(
        lambda **kw: read_team_activity(client, token=token, team_id=team.id, since=boundary, **kw),
        limit=2,
    )
    assert {row["id"] for row in ranged} == since_ids


async def test_a_narrowing_filter_can_only_remove_rows(
    client: AsyncClient, db: AsyncSession
) -> None:
    """`actor` narrows an already team-scoped set; it never widens it.

    A member of another team, or an id that does not exist, produces an empty page — not a
    window into somebody else's history.
    """
    team, users = await scenario(
        db, roles={"member": Role.MEMBER, "other": Role.MEMBER}, team_key="alpha"
    )
    token = await login(client, users["member"].email)
    other_token = await login(client, users["other"].email)
    mine = await create_item_via_api(client, token=token, team_id=team.id, title="Mine")
    theirs = await create_item_via_api(client, token=other_token, team_id=team.id, title="Theirs")

    as_me = await read_team_activity(client, token=token, team_id=team.id, actor="me")
    as_other = await read_team_activity(
        client, token=token, team_id=team.id, actor=str(users["other"].id)
    )
    as_nobody = await read_team_activity(
        client, token=token, team_id=team.id, actor=str(uuid.uuid4())
    )

    assert as_me.status_code == as_other.status_code == as_nobody.status_code == 200
    assert {event["work_item_id"] for event in as_me.json()["items"]} == {mine["id"]}
    assert {event["work_item_id"] for event in as_other.json()["items"]} == {theirs["id"]}
    assert as_nobody.json()["items"] == []
    assert as_nobody.json()["has_more"] is False


async def test_a_bad_date_range_or_actor_is_a_validation_error(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER}, team_key="alpha")
    token = await login(client, users["member"].email)
    now = datetime.now(UTC)

    inverted = await read_team_activity(
        client,
        token=token,
        team_id=team.id,
        since=now.isoformat(),
        until=(now - timedelta(hours=1)).isoformat(),
    )
    naive = await read_team_activity(
        client, token=token, team_id=team.id, since="2026-10-17T00:00:00"
    )
    garbage = await read_team_activity(
        client, token=token, team_id=team.id, since="the day before yesterday"
    )
    bad_actor = await read_team_activity(client, token=token, team_id=team.id, actor="someone")

    for response in (inverted, naive, garbage, bad_actor):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "validation_failed"
    assert "since" in naive.json()["error"]["message"]
    assert "offset" in naive.json()["error"]["message"]
