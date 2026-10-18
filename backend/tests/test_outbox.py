"""The transactional outbox: queued in the mutation, delivered by a worker.

What these tests are actually defending
---------------------------------------
It is easy to write an outbox that *looks* right and is not: enqueue after the commit, and
a crash silently loses the notification; enqueue in a different transaction, and a
rolled-back assignment still notifies somebody. So the coupling is tested by **breaking
the outbox insert on purpose** and checking that the assignment disappears with it, the
same way `test_activity_atomicity.py` breaks the history insert.

The delivery half is tested for the properties that make an at-least-once queue safe:
duplicate processing creates one notification, `FOR UPDATE SKIP LOCKED` stops two workers
taking the same row, and retry exhaustion is a terminal, visible state rather than a
silent disappearance.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from app.models.enums import Role
from app.services import outbox
from app.worker import process_batch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import (
    api_patch,
    auth_headers,
    claim_ok,
    count_notifications,
    count_outbox,
    create_item_via_api,
    login,
    make_outbox_due,
    move_to,
    notification_rows,
    outbox_rows,
    raw_item,
    scenario,
)

# asyncpg prepares one statement per round trip, so these run individually.
BREAK_OUTBOX = (
    """
    CREATE OR REPLACE FUNCTION test_fail_outbox_insert() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'outbox_events insert deliberately failed (test)';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS test_fail_outbox_insert ON outbox_events",
    """
    CREATE TRIGGER test_fail_outbox_insert
        BEFORE INSERT ON outbox_events
        FOR EACH ROW EXECUTE FUNCTION test_fail_outbox_insert()
    """,
)

REPAIR_OUTBOX = (
    "DROP TRIGGER IF EXISTS test_fail_outbox_insert ON outbox_events",
    "DROP FUNCTION IF EXISTS test_fail_outbox_insert()",
)


async def _run(db: AsyncSession, statements: tuple[str, ...]) -> None:
    for statement in statements:
        await db.execute(text(statement))
    await db.commit()


async def break_outbox(db: AsyncSession) -> None:
    await _run(db, BREAK_OUTBOX)


async def repair_outbox(db: AsyncSession) -> None:
    await _run(db, REPAIR_OUTBOX)


@pytest.fixture(autouse=True)
async def _never_leak_the_trigger(db: AsyncSession) -> AsyncIterator[None]:
    yield
    await repair_outbox(db)


@pytest.fixture
async def failure_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client that receives the application's 500 instead of the raised exception.

    The shared client re-raises unhandled errors, which is useful when debugging but is
    not what a caller sees. These tests assert on what the caller is told when the outbox
    cannot be written.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


# --------------------------------------------------------------------------- #
# What gets queued, and what does not
# --------------------------------------------------------------------------- #


async def test_assigning_to_somebody_queues_one_event(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)

    item = await create_item_via_api(
        client,
        token=lead,
        team_id=team.id,
        title="Needs an owner",
        assignee_id=str(users["member"].id),
    )

    rows = await outbox_rows(db)
    assert [row["event_type"] for row in rows] == ["work_item_assigned"]
    assert rows[0]["status"] == "pending"
    assert rows[0]["aggregate_id"] == uuid.UUID(item["id"])
    assert rows[0]["payload"]["recipient_id"] == str(users["member"].id)
    assert rows[0]["payload"]["work_item_title"] == "Needs an owner"


async def test_self_assignment_queues_nothing(client: AsyncClient, db: AsyncSession) -> None:
    """Telling somebody about their own action is noise, so it is never queued."""
    team, users = await scenario(db, roles={"member": Role.MEMBER})
    token = await login(client, users["member"].email)

    await create_item_via_api(
        client,
        token=token,
        team_id=team.id,
        title="Mine already",
        assignee_id=str(users["member"].id),
    )

    assert await count_outbox(db) == 0


async def test_creating_unassigned_queues_nothing(client: AsyncClient, db: AsyncSession) -> None:
    team, users = await scenario(db, roles={"member": Role.MEMBER})
    token = await login(client, users["member"].email)

    await create_item_via_api(client, token=token, team_id=team.id, title="Nobody's yet")

    assert await count_outbox(db) == 0


async def test_claiming_queues_nothing(client: AsyncClient, db: AsyncSession) -> None:
    """A claim is the caller taking their own work; the feed already shows it."""
    team, users = await scenario(db, roles={"member": Role.MEMBER})
    token = await login(client, users["member"].email)
    item = await create_item_via_api(client, token=token, team_id=team.id, title="Up for grabs")

    await claim_ok(client, token=token, item_id=item["id"])

    assert await count_outbox(db) == 0


async def test_reassigning_queues_an_event_for_the_new_assignee(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    item = await create_item_via_api(client, token=lead, team_id=team.id, title="Move me")

    response = await api_patch(
        client,
        token=lead,
        item_id=item["id"],
        etag='"1"',
        assignee_id=str(users["member"].id),
    )
    assert response.status_code == 200, response.text

    rows = await outbox_rows(db)
    assert [row["event_type"] for row in rows] == ["work_item_assigned"]
    assert rows[0]["payload"]["recipient_id"] == str(users["member"].id)


async def test_unassigning_queues_nothing(client: AsyncClient, db: AsyncSession) -> None:
    """The person losing the work is not notified: the feed records it, and the
    notification would arrive after the work has already moved."""
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    item = await create_item_via_api(
        client,
        token=lead,
        team_id=team.id,
        title="Briefly owned",
        assignee_id=str(users["member"].id),
    )
    assert await count_outbox(db) == 1

    response = await api_patch(client, token=lead, item_id=item["id"], etag='"1"', assignee_id=None)
    assert response.status_code == 200, response.text

    assert await count_outbox(db) == 1, "unassigning must not queue another event"


async def test_approval_queues_an_approval_event(client: AsyncClient, db: AsyncSession) -> None:
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    member = await login(client, users["member"].email)
    lead = await login(client, users["lead"].email)

    item = await create_item_via_api(client, token=member, team_id=team.id, title="Please review")
    item_id = item["id"]
    await claim_ok(client, token=member, item_id=item_id)
    await move_to(client, token=member, item_id=item_id, target="in_progress")
    await move_to(client, token=member, item_id=item_id, target="awaiting_approval")
    assert await count_outbox(db) == 0, "the steps up to review notify nobody"

    await move_to(client, token=lead, item_id=item_id, target="resolved")

    rows = await outbox_rows(db)
    assert [row["event_type"] for row in rows] == ["work_item_approved"]
    assert rows[0]["payload"]["recipient_id"] == str(users["member"].id)


async def test_rejection_queues_a_rejection_event(client: AsyncClient, db: AsyncSession) -> None:
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    member = await login(client, users["member"].email)
    lead = await login(client, users["lead"].email)

    item = await create_item_via_api(client, token=member, team_id=team.id, title="Needs rework")
    item_id = item["id"]
    await claim_ok(client, token=member, item_id=item_id)
    await move_to(client, token=member, item_id=item_id, target="in_progress")
    await move_to(client, token=member, item_id=item_id, target="awaiting_approval")

    await move_to(client, token=lead, item_id=item_id, target="in_progress", note="Not yet")

    rows = await outbox_rows(db)
    assert [row["event_type"] for row in rows] == ["work_item_rejected"]
    assert rows[0]["payload"]["recipient_id"] == str(users["member"].id)


async def test_a_stale_update_queues_nothing(client: AsyncClient, db: AsyncSession) -> None:
    """The event is written after the conditional update, so a refused update has none.

    This is the ordering that matters: queueing before the version check would notify
    somebody about an assignment that never happened.
    """
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    item = await create_item_via_api(client, token=lead, team_id=team.id, title="Do not move")

    response = await api_patch(
        client,
        token=lead,
        item_id=item["id"],
        etag='"99"',
        assignee_id=str(users["member"].id),
    )

    assert response.status_code == 412
    assert await count_outbox(db) == 0
    assert (await raw_item(db, uuid.UUID(item["id"])))["assignee_id"] is None


# --------------------------------------------------------------------------- #
# Same transaction as the change
# --------------------------------------------------------------------------- #


async def test_a_failed_outbox_write_rolls_back_an_assignment(
    failure_client: AsyncClient, db: AsyncSession
) -> None:
    """If the notification cannot be queued, the assignment does not happen either.

    The alternative — assign successfully, then fail to queue — is exactly the silent
    loss the outbox exists to prevent.
    """
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(failure_client, users["lead"].email)
    item = await create_item_via_api(
        failure_client, token=lead, team_id=team.id, title="Stays free"
    )
    before = await raw_item(db, uuid.UUID(item["id"]))

    await break_outbox(db)
    response = await api_patch(
        failure_client,
        token=lead,
        item_id=item["id"],
        etag='"1"',
        assignee_id=str(users["member"].id),
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    for leak in ("outbox_events", "test_fail_outbox_insert", "RAISE", "asyncpg", "Traceback"):
        assert leak not in response.text, leak

    after = await raw_item(db, uuid.UUID(item["id"]))
    assert after == before, "the assignment survived even though its event could not be queued"
    assert await count_outbox(db) == 0


async def test_a_failed_outbox_write_rolls_back_a_creation(
    failure_client: AsyncClient, db: AsyncSession
) -> None:
    """A create that assigns somebody is one unit: no item without its event."""
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(failure_client, users["lead"].email)
    await break_outbox(db)

    response = await failure_client.post(
        f"/api/v1/teams/{team.id}/items",
        json={"title": "Never appears", "type": "task", "assignee_id": str(users["member"].id)},
        headers=auth_headers(lead) | {"Idempotency-Key": str(uuid.uuid4())},
    )

    assert response.status_code == 500
    count = (await db.execute(text("SELECT count(*) FROM work_items"))).scalar_one()
    assert count == 0, "the item was created even though its event could not be queued"
    assert await count_outbox(db) == 0
    # The idempotency key rolled back with it, so a retry is not blocked by a dead key.
    keys = (await db.execute(text("SELECT count(*) FROM idempotency_keys"))).scalar_one()
    assert keys == 0


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


async def test_delivery_writes_one_notification_and_marks_the_event(
    client: AsyncClient, db: AsyncSession
) -> None:
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    await create_item_via_api(
        client,
        token=lead,
        team_id=team.id,
        title="Deliver this",
        assignee_id=str(users["member"].id),
    )

    processed = await process_batch(db)

    assert processed == 1
    notifications = await notification_rows(db, users["member"].id)
    assert len(notifications) == 1
    assert notifications[0]["event_type"] == "work_item_assigned"
    assert "Deliver this" in notifications[0]["body"]
    assert notifications[0]["read_at"] is None
    assert (await outbox_rows(db))[0]["status"] == "delivered"


async def test_replaying_a_delivered_event_creates_no_second_notification(
    client: AsyncClient, db: AsyncSession
) -> None:
    """The at-least-once half is allowed to happen; the exactly-once half is the schema.

    Requeues the delivered event, which is what an operator replay or a fencepost bug
    would do, and proves the unique constraint turns the second delivery into a no-op.
    """
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    await create_item_via_api(
        client,
        token=lead,
        team_id=team.id,
        title="Only once",
        assignee_id=str(users["member"].id),
    )
    await process_batch(db)
    assert await count_notifications(db) == 1

    await db.execute(
        text(
            "UPDATE outbox_events SET status = 'pending', processed_at = NULL "
            "WHERE status = 'delivered'"
        )
    )
    await db.commit()

    await process_batch(db)

    assert await count_notifications(db) == 1, "the same event produced a second notification"
    assert (await outbox_rows(db))[0]["status"] == "delivered"


async def test_a_departed_recipient_gets_nothing_and_the_event_is_not_stuck(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Leaving the team makes the notification unnecessary, not undeliverable.

    Failure here would retry five times and then sit in `failed` forever, which would make
    a routine event look like an incident.
    """
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    item = await create_item_via_api(
        client,
        token=lead,
        team_id=team.id,
        title="Addressed to somebody who left",
        assignee_id=str(users["member"].id),
    )
    # Unassign, then remove the membership directly: the API refuses to remove a member
    # who still owns work, but the point here is what delivery does once they are gone.
    await api_patch(client, token=lead, item_id=item["id"], etag='"1"', assignee_id=None)
    await db.execute(
        text("DELETE FROM memberships WHERE user_id = :user AND team_id = :team"),
        {"user": users["member"].id, "team": team.id},
    )
    await db.commit()

    processed = await process_batch(db)

    assert processed == 1
    assert await count_notifications(db) == 0
    assert (await outbox_rows(db))[0]["status"] == "delivered"


async def test_retry_exhaustion_is_terminal_and_visible(
    client: AsyncClient, db: AsyncSession
) -> None:
    """A delivery that cannot succeed stops being retried and says so.

    `failed` is the only terminal state, so a non-zero count there means an event needs a
    human. Without it, an event that gave up is indistinguishable from one still working.
    """
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    await create_item_via_api(
        client,
        token=lead,
        team_id=team.id,
        title="Undeliverable",
        assignee_id=str(users["member"].id),
    )

    async def always_fails(session: AsyncSession, event: object) -> bool:
        raise RuntimeError("delivery channel unavailable")

    first = await process_batch(db, handler=always_fails, max_attempts=2)
    assert first == 1
    row = (await outbox_rows(db))[0]
    assert row["status"] == "pending", "a retryable failure must stay pending"
    assert row["attempts"] == 1
    assert "delivery channel unavailable" in row["last_error"]
    scheduled_in_the_future = (
        await db.execute(text("SELECT next_attempt_at > now() FROM outbox_events"))
    ).scalar_one()
    assert scheduled_in_the_future is True, "the backoff must schedule the retry, not loop"

    await make_outbox_due(db)
    await process_batch(db, handler=always_fails, max_attempts=2)

    row = (await outbox_rows(db))[0]
    assert row["status"] == "failed", "the event should be terminal after the last attempt"
    assert row["attempts"] == 2
    assert row["processed_at"] is not None
    assert "delivery channel unavailable" in row["last_error"]
    assert await count_notifications(db) == 0


def test_backoff_grows_then_caps() -> None:
    """Bounded exponential: a retry that waits an hour is a retry that has given up."""
    delays = [outbox.backoff_seconds(attempt) for attempt in range(0, 9)]
    assert delays[0] == 0
    assert delays[1:7] == [2, 4, 8, 16, 32, 60]
    assert delays[7] == 60
    assert delays[8] == 60


async def test_two_workers_cannot_claim_the_same_event(app: FastAPI, db: AsyncSession) -> None:
    """`FOR UPDATE SKIP LOCKED`: the second worker takes other work, not this row.

    Two independent connections, because a shared session would hide the lock. The second
    claim must return nothing and must not block waiting for the first.
    """
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    item = await db.execute(
        text(
            "INSERT INTO work_items (team_id, title, type, priority, created_by) "
            "VALUES (:team, 'Locked work', 'task', 'medium', :user) RETURNING id"
        ),
        {"team": team.id, "user": users["lead"].id},
    )
    item_id = item.scalar_one()
    await db.execute(
        text(
            "INSERT INTO outbox_events (event_type, aggregate_id, team_id, payload) "
            "VALUES ('work_item_assigned', :item, :team, '{}'::jsonb)"
        ),
        {"item": item_id, "team": team.id},
    )
    await db.commit()

    factory = app.state.db.session_factory
    async with factory() as first_session, factory() as second_session:
        first = await outbox.claim_batch(first_session, limit=10)
        second = await outbox.claim_batch(second_session, limit=10)

        assert len(first) == 1
        assert second == [], "the second worker claimed a row the first one holds"

        await first_session.rollback()
        await second_session.rollback()

    # Once released, the row is available again — the lock prevented overlap, not delivery.
    async with factory() as third_session:
        third = await outbox.claim_batch(third_session, limit=10)
        assert len(third) == 1
        await third_session.rollback()


async def test_a_bad_payload_fails_observably(client: AsyncClient, db: AsyncSession) -> None:
    """A producer bug surfaces as a failed event with its reason, not a silent drop."""
    team, users = await scenario(db, roles={"lead": Role.LEAD, "member": Role.MEMBER})
    lead = await login(client, users["lead"].email)
    await create_item_via_api(
        client,
        token=lead,
        team_id=team.id,
        title="Bad payload",
        assignee_id=str(users["member"].id),
    )
    # Remove the fact the body needs, as a producer regression would.
    await db.execute(text("UPDATE outbox_events SET payload = '{\"recipient_id\": null}'::jsonb"))
    await db.commit()

    await process_batch(db, max_attempts=1)

    row = (await outbox_rows(db))[0]
    assert row["status"] == "failed"
    assert "recipient_id" in row["last_error"]
    assert await count_notifications(db) == 0
