"""Inventory of indexes and named constraints.

Why this exists: ``alembic check`` (used in ``tests/test_migrations.py``) compares
tables, columns, indexes, and foreign keys, but it does **not** compare ``CHECK``
constraint bodies. A check constraint changed in a model and not in a migration
would therefore slip through the drift check. These assertions close that gap for
the constraints that protect real invariants, and they also make the indexing
decisions visible to a reviewer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# index name -> substring that must appear in its definition
EXPECTED_INDEXES: dict[str, str] = {
    # Team-scoped, status-filtered lists, newest first, with `id` as tie-breaker.
    "ix_work_items_team_status_created": "created_at DESC, id DESC",
    # The same list without a status filter (the default team view). Added by migration
    # 0002 after measuring that the index above cannot serve it.
    "ix_work_items_team_created": "created_at DESC, id DESC",
    # "assigned to me" across teams.
    "ix_work_items_assignee_created": "created_at DESC, id DESC",
    # Partial index: only rows the unassigned-urgent queue can return.
    "ix_work_items_unassigned_urgent": "WHERE",
    # Full-text search.
    "ix_work_items_search_vector": "gin",
    # Per-item timelines, newest first.
    "ix_activity_events_work_item_id_created_at": "created_at DESC, id DESC",
    "ix_activity_events_team_id_created_at": "created_at DESC, id DESC",
    # Per-item comment threads, oldest first.
    "ix_comments_work_item_id_created_at": "work_item_id, created_at, id",
    # "which teams does this user belong to" (the prefix the composite unique
    # constraint cannot answer).
    "ix_memberships_user_id": "user_id",
    # Token lookup and "revoke every session for this user".
    "ix_auth_sessions_user_id": "user_id",
    # The outbox worker's claim query. Partial, so the index is proportional to the
    # backlog rather than to everything ever delivered.
    "ix_outbox_events_pending": "WHERE",
    # A person's notification list, newest first, keyset-paged like every other list.
    "ix_notifications_recipient_created": "created_at DESC, id DESC",
    # The unread badge. Partial: it only ever asks about rows that can be unread.
    "ix_notifications_recipient_unread": "WHERE",
}

EXPECTED_CONSTRAINTS: dict[str, str] = {
    # c = check, f = foreign key, u = unique, p = primary key
    "ck_work_items_status_is_known": "c",
    "ck_work_items_priority_is_known": "c",
    "ck_work_items_type_is_known": "c",
    "ck_work_items_version_positive": "c",
    "ck_work_items_title_length": "c",
    "ck_work_items_description_length": "c",
    "ck_memberships_role_is_known": "c",
    "ck_comments_body_length": "c",
    "ck_activity_events_event_type_is_known": "c",
    "ck_idempotency_keys_status_is_known": "c",
    "ck_users_email_has_at_sign": "c",
    # The invariant that makes "assignee belongs to the item's team" a database
    # rule rather than an application convention.
    "fk_work_items_team_id_assignee_id_memberships": "f",
    # The duplicate-request guard.
    "uq_idempotency_keys_scope": "u",
    # One role per user per team, and the target of the composite foreign key.
    "uq_memberships_team_id_user_id": "u",
    "uq_auth_sessions_token_hash": "u",
    "uq_users_email": "u",
    "uq_teams_key": "u",
    "ck_outbox_events_event_type_is_known": "c",
    "ck_outbox_events_status_is_known": "c",
    "ck_outbox_events_attempts_not_negative": "c",
    "ck_notifications_event_type_is_known": "c",
    "ck_notifications_body_length": "c",
    "fk_outbox_events_team_id_teams": "f",
    "fk_notifications_outbox_event_id_outbox_events": "f",
    "fk_notifications_team_id_recipient_id_memberships": "f",
    # At-least-once delivery, exactly-once effect: one event, one notification.
    "uq_notifications_outbox_event_id": "u",
}


async def test_expected_indexes_exist(db: AsyncSession) -> None:
    result = await db.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
    )
    definitions = {row.indexname: row.indexdef for row in result}

    missing = sorted(set(EXPECTED_INDEXES) - set(definitions))
    assert missing == [], f"missing indexes: {missing}"


@pytest.mark.parametrize(("index_name", "fragment"), sorted(EXPECTED_INDEXES.items()))
async def test_index_definition_matches_intent(
    db: AsyncSession, index_name: str, fragment: str
) -> None:
    definition = (
        await db.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"
            ),
            {"name": index_name},
        )
    ).scalar_one()

    assert fragment.lower() in definition.lower()


async def test_expected_named_constraints_exist(db: AsyncSession) -> None:
    result = await db.execute(
        text(
            """
            SELECT conname, contype::text AS contype
            FROM pg_constraint
            WHERE connamespace = 'public'::regnamespace
            """
        )
    )
    found = {row.conname: row.contype for row in result}

    missing = sorted(set(EXPECTED_CONSTRAINTS) - set(found))
    assert missing == [], f"missing constraints: {missing}"

    wrong_type = {
        name: (expected, found[name])
        for name, expected in EXPECTED_CONSTRAINTS.items()
        if found[name] != expected
    }
    assert wrong_type == {}


async def test_unassigned_urgent_index_is_partial(db: AsyncSession) -> None:
    """A partial index is only a win if the predicate is actually restrictive."""
    definition = (
        await db.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = 'ix_work_items_unassigned_urgent'"
            )
        )
    ).scalar_one()

    normalised = definition.lower()
    assert "where" in normalised
    assert "assignee_id is null" in normalised
    assert "status" in normalised and "priority" in normalised


async def test_outbox_pending_index_is_partial(db: AsyncSession) -> None:
    """The worker only claims pending rows, so the index must only hold those."""
    definition = (
        await db.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = 'ix_outbox_events_pending'"
            )
        )
    ).scalar_one()

    normalised = definition.lower()
    assert "where" in normalised
    assert "pending" in normalised
    assert "next_attempt_at" in normalised
