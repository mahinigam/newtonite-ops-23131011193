"""Database-level invariants.

These tests bypass the application entirely and talk to PostgreSQL directly.
That is the point: they prove the guarantees hold even if application code is
wrong, or a human connects with `psql` and tries the same thing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.support import (
    TEAM_ALPHA,
    USER_ALPHA_MEMBER,
    USER_BETA_MEMBER,
    seed_two_teams,
)


async def _seed(db: AsyncSession) -> None:
    await seed_two_teams(db)


_INSERT_ITEM = text(
    """
    INSERT INTO work_items (team_id, title, type, priority, assignee_id, created_by)
    VALUES (:team, :title, 'task', 'medium', :assignee, :created_by)
    """
)


async def test_assignee_from_the_same_team_is_accepted(db: AsyncSession) -> None:
    await _seed(db)

    await db.execute(
        _INSERT_ITEM,
        {
            "team": TEAM_ALPHA,
            "title": "A valid title",
            "assignee": USER_ALPHA_MEMBER,
            "created_by": USER_ALPHA_MEMBER,
        },
    )
    await db.commit()

    assignee = (await db.execute(text("SELECT assignee_id FROM work_items"))).scalar_one()
    assert str(assignee) == USER_ALPHA_MEMBER


async def test_assignee_from_another_team_is_rejected_by_the_database(
    db: AsyncSession,
) -> None:
    """`work_items(team_id, assignee_id) -> memberships(team_id, user_id)`.

    An assignee who is not a member of the item's team must be impossible, not
    merely discouraged by application validation.
    """
    await _seed(db)

    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            _INSERT_ITEM,
            {
                "team": TEAM_ALPHA,
                "title": "A valid title",
                "assignee": USER_BETA_MEMBER,
                "created_by": USER_ALPHA_MEMBER,
            },
        )
        await db.commit()

    assert "fk_work_items_team_id_assignee_id_memberships" in str(excinfo.value)
    await db.rollback()


async def test_unassigned_item_is_allowed(db: AsyncSession) -> None:
    await _seed(db)

    await db.execute(
        _INSERT_ITEM,
        {
            "team": TEAM_ALPHA,
            "title": "A valid title",
            "assignee": None,
            "created_by": USER_ALPHA_MEMBER,
        },
    )
    await db.commit()

    count = (await db.execute(text("SELECT count(*) FROM work_items"))).scalar_one()
    assert count == 1


async def test_title_length_is_enforced(db: AsyncSession) -> None:
    await _seed(db)

    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            _INSERT_ITEM,
            {
                "team": TEAM_ALPHA,
                "title": "ab",
                "assignee": None,
                "created_by": USER_ALPHA_MEMBER,
            },
        )
        await db.commit()

    assert "ck_work_items_title_length" in str(excinfo.value)
    await db.rollback()


async def test_status_is_constrained(db: AsyncSession) -> None:
    await _seed(db)

    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            text(
                """
                INSERT INTO work_items (team_id, title, type, priority, status, created_by)
                VALUES (:team, 'Some valid title', 'task', 'medium', 'not_a_status', :created_by)
                """
            ),
            {"team": TEAM_ALPHA, "created_by": USER_ALPHA_MEMBER},
        )
        await db.commit()

    assert "ck_work_items_status_is_known" in str(excinfo.value)
    await db.rollback()


async def test_one_role_per_user_per_team(db: AsyncSession) -> None:
    await _seed(db)

    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            text("INSERT INTO memberships (user_id, team_id, role) VALUES (:user, :team, 'lead')"),
            {"user": USER_ALPHA_MEMBER, "team": TEAM_ALPHA},
        )
        await db.commit()

    assert "uq_memberships_team_id_user_id" in str(excinfo.value)
    await db.rollback()


async def test_activity_history_is_append_only(db: AsyncSession) -> None:
    """The audit trail cannot be rewritten, even by direct SQL."""
    await _seed(db)
    await db.execute(
        _INSERT_ITEM,
        {
            "team": TEAM_ALPHA,
            "title": "A valid title",
            "assignee": None,
            "created_by": USER_ALPHA_MEMBER,
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO activity_events (work_item_id, team_id, actor_id, event_type, to_value)
            SELECT id, team_id, created_by, 'created', 'open' FROM work_items
            """
        )
    )
    await db.commit()

    with pytest.raises(DBAPIError) as update_error:
        await db.execute(text("UPDATE activity_events SET event_type = 'closed'"))
        await db.commit()
    assert "append-only" in str(update_error.value)
    await db.rollback()

    with pytest.raises(DBAPIError) as delete_error:
        await db.execute(text("DELETE FROM activity_events"))
        await db.commit()
    assert "append-only" in str(delete_error.value)
    await db.rollback()

    surviving = (await db.execute(text("SELECT count(*) FROM activity_events"))).scalar_one()
    assert surviving == 1


# --------------------------------------------------------------------------- #
# The outbox and notifications
# --------------------------------------------------------------------------- #

_QUEUE_EVENT = text(
    """
    INSERT INTO outbox_events (event_type, aggregate_id, team_id, payload)
    VALUES ('work_item_assigned', :item, :team, '{}'::jsonb)
    RETURNING id
    """
)

_INSERT_NOTIFICATION = text(
    """
    INSERT INTO notifications
        (outbox_event_id, recipient_id, team_id, work_item_id, event_type, body)
    VALUES (:event, :recipient, :team, :item, 'work_item_assigned', 'A body')
    """
)


async def _item_and_event(db: AsyncSession) -> tuple[object, int]:
    """Seed, add one unassigned item in alpha, and queue one event about it."""
    await _seed(db)
    item_id = (
        await db.execute(
            text(
                """
                INSERT INTO work_items (team_id, title, type, priority, created_by)
                VALUES (:team, 'Notifiable work', 'task', 'medium', :created_by)
                RETURNING id
                """
            ),
            {"team": TEAM_ALPHA, "created_by": USER_ALPHA_MEMBER},
        )
    ).scalar_one()
    event_id = (await db.execute(_QUEUE_EVENT, {"item": item_id, "team": TEAM_ALPHA})).scalar_one()
    await db.commit()
    return item_id, int(event_id)


async def test_a_notification_requires_a_recipient_in_the_team(db: AsyncSession) -> None:
    """`notifications(team_id, recipient_id) -> memberships(team_id, user_id)`.

    A notification about alpha addressed to a beta-only member must be impossible, not
    merely discouraged by the consumer. History is team-scoped; so is being told about it.
    """
    item_id, event_id = await _item_and_event(db)
    await db.rollback()

    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            _INSERT_NOTIFICATION,
            {
                "event": event_id,
                "recipient": USER_BETA_MEMBER,
                "team": TEAM_ALPHA,
                "item": item_id,
            },
        )
        await db.commit()

    assert "fk_notifications_team_id_recipient_id_memberships" in str(excinfo.value)
    await db.rollback()


async def test_the_same_outbox_event_cannot_produce_two_notifications(
    db: AsyncSession,
) -> None:
    """Exactly-once *effect* for an at-least-once queue: the constraint is the guarantee."""
    item_id, event_id = await _item_and_event(db)

    await db.execute(
        _INSERT_NOTIFICATION,
        {
            "event": event_id,
            "recipient": USER_ALPHA_MEMBER,
            "team": TEAM_ALPHA,
            "item": item_id,
        },
    )
    await db.commit()

    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            _INSERT_NOTIFICATION,
            {
                "event": event_id,
                "recipient": USER_ALPHA_MEMBER,
                "team": TEAM_ALPHA,
                "item": item_id,
            },
        )
        await db.commit()

    assert "uq_notifications_outbox_event_id" in str(excinfo.value)
    await db.rollback()

    count = (await db.execute(text("SELECT count(*) FROM notifications"))).scalar_one()
    assert count == 1


async def test_leaving_a_team_removes_the_notifications_about_it(db: AsyncSession) -> None:
    """A notification has no reader once the membership is gone.

    The cascade is deliberate: unlike `activity_events`, this table is disposable delivery
    state, and leaving it behind would let a removed member keep reading item titles.
    """
    item_id, event_id = await _item_and_event(db)
    await db.execute(
        _INSERT_NOTIFICATION,
        {
            "event": event_id,
            "recipient": USER_ALPHA_MEMBER,
            "team": TEAM_ALPHA,
            "item": item_id,
        },
    )
    await db.commit()

    await db.execute(
        text("DELETE FROM memberships WHERE user_id = :user AND team_id = :team"),
        {"user": USER_ALPHA_MEMBER, "team": TEAM_ALPHA},
    )
    await db.commit()

    count = (await db.execute(text("SELECT count(*) FROM notifications"))).scalar_one()
    assert count == 0


async def test_outbox_status_is_constrained(db: AsyncSession) -> None:
    """`failed` is terminal and meaningful, so an invented status must not be storable."""
    item_id, _event_id = await _item_and_event(db)

    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            text(
                """
                INSERT INTO outbox_events (event_type, aggregate_id, team_id, status)
                VALUES ('work_item_assigned', :item, :team, 'not_a_status')
                """
            ),
            {"item": item_id, "team": TEAM_ALPHA},
        )
        await db.commit()

    assert "ck_outbox_events_status_is_known" in str(excinfo.value)
    await db.rollback()


async def test_outbox_event_type_is_constrained(db: AsyncSession) -> None:
    item_id, _event_id = await _item_and_event(db)

    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            text(
                """
                INSERT INTO outbox_events (event_type, aggregate_id, team_id)
                VALUES ('work_item_deleted', :item, :team)
                """
            ),
            {"item": item_id, "team": TEAM_ALPHA},
        )
        await db.commit()

    assert "ck_outbox_events_event_type_is_known" in str(excinfo.value)
    await db.rollback()


async def test_a_notification_must_say_something(db: AsyncSession) -> None:
    item_id, event_id = await _item_and_event(db)

    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            text(
                """
                INSERT INTO notifications
                    (outbox_event_id, recipient_id, team_id, work_item_id, event_type, body)
                VALUES (:event, :recipient, :team, :item, 'work_item_assigned', '')
                """
            ),
            {
                "event": event_id,
                "recipient": USER_ALPHA_MEMBER,
                "team": TEAM_ALPHA,
                "item": item_id,
            },
        )
        await db.commit()

    assert "ck_notifications_body_length" in str(excinfo.value)
    await db.rollback()
