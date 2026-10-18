"""add the transactional outbox and in-app notifications

Two tables, and the relationship between them is the point.

`outbox_events` is written **inside the transaction that causes it** — an assignment, an
approval, a rejection. `notifications` is written later by a worker, addressed to one
person, with a `read_at` because a person can read it.

The split is what makes delivery safe. If the mutation rolls back, the outbox row rolls
back with it, so nothing is ever delivered for a change that did not happen. If the
worker crashes mid-delivery, its transaction rolls back and the row stays pending for the
next poll. Delivery is therefore at-least-once, and the unique constraint on
`notifications.outbox_event_id` makes the *effect* exactly-once: processing the same event
twice inserts one notification, not two.

Indexes are added only for queries that exist:

* `ix_outbox_events_pending` covers the worker's claim query —
  `status = 'pending' AND next_attempt_at <= now() ORDER BY next_attempt_at, id`.
  Partial, so it stays proportional to the backlog rather than to everything ever
  delivered.
* `ix_notifications_recipient_created` is the notification list, newest first.
* `ix_notifications_recipient_unread` is the unread badge, partial on
  `read_at IS NULL` for the same reason.

Two integrity rules are enforced here rather than in application code:

* `notifications.outbox_event_id` is unique, which is what makes duplicate processing
  harmless.
* `(team_id, recipient_id) -> memberships(team_id, user_id)` refuses a notification whose
  recipient is not in the team it describes. It cascades, because a notification has no
  reader once the membership is gone; history lives in `activity_events`, which is
  append-only and unaffected.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "aggregate_type",
            sa.String(length=32),
            server_default=sa.text("'work_item'"),
            nullable=False,
        ),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('work_item_assigned', 'work_item_approved', 'work_item_rejected')",
            name="event_type_is_known",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')",
            name="status_is_known",
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_outbox_events_team_id_teams",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
    )
    # The worker's claim query: pending rows, oldest due first.
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["next_attempt_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("outbox_event_id", sa.BigInteger(), nullable=False),
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('work_item_assigned', 'work_item_approved', 'work_item_rejected')",
            name="event_type_is_known",
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 500",
            name="body_length",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_event_id"],
            ["outbox_events.id"],
            name="fk_notifications_outbox_event_id_outbox_events",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["users.id"],
            name="fk_notifications_recipient_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name="fk_notifications_team_id_teams",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name="fk_notifications_work_item_id_work_items",
            ondelete="RESTRICT",
        ),
        # The recipient must be a member of the team the notification is about.
        sa.ForeignKeyConstraint(
            ["team_id", "recipient_id"],
            ["memberships.team_id", "memberships.user_id"],
            name="fk_notifications_team_id_recipient_id_memberships",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        # Duplicate delivery of one outbox event creates at most one notification.
        sa.UniqueConstraint("outbox_event_id", name="uq_notifications_outbox_event_id"),
    )
    op.create_index(
        "ix_notifications_recipient_created",
        "notifications",
        ["recipient_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_notifications_recipient_unread",
        "notifications",
        ["recipient_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notifications_recipient_unread",
        table_name="notifications",
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.drop_index("ix_notifications_recipient_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index(
        "ix_outbox_events_pending",
        table_name="outbox_events",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("outbox_events")
