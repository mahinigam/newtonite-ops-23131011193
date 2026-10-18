"""In-app notifications, delivered from the outbox.

A notification is a **delivery record addressed to one person**, not history. That
distinction decides three things:

* It is written by the worker, not by the request that caused it, so a slow or failing
  delivery channel can never fail the assignment itself.
* It has a `read_at`, so it is mutable — unlike `activity_events`, which is append-only.
* `UNIQUE (outbox_event_id)` makes delivery idempotent: the outbox is at-least-once, and
  this constraint turns a duplicate delivery into a no-op instead of a second
  notification. That is the whole reason the two tables exist rather than the worker
  trusting its own bookkeeping.

The composite foreign key `(team_id, recipient_id) -> memberships(team_id, user_id)` is
the same invariant `work_items` uses: a person can only be notified about work in a team
they belong to. It cascades, because a notification has no reader once the membership is
gone — and leaving it behind would let a removed member keep reading item titles. The
audit trail is `activity_events`, which is append-only and team-scoped; nothing about
history depends on this table.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import OutboxEventType, check_in

if TYPE_CHECKING:
    from app.models.user import User


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # The event that caused this notification. Unique, which is what makes duplicate
    # processing harmless.
    outbox_event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("outbox_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Rendering-ready text, built by the consumer from the outbox payload. Storing it
    # means the notification reads the same even after the item is renamed, which is
    # right: it records what the person was told.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recipient: Mapped[User] = relationship(lazy="raise")

    __table_args__ = (
        UniqueConstraint("outbox_event_id", name="uq_notifications_outbox_event_id"),
        # A recipient must belong to the team the notification is about. This is the
        # database refusing to create a notification that leaks one team's work to
        # somebody outside it, even if a future producer gets the recipient wrong.
        ForeignKeyConstraint(
            ["team_id", "recipient_id"],
            ["memberships.team_id", "memberships.user_id"],
            name="fk_notifications_team_id_recipient_id_memberships",
            ondelete="CASCADE",
        ),
        CheckConstraint(check_in("event_type", OutboxEventType), name="event_type_is_known"),
        CheckConstraint("char_length(body) BETWEEN 1 AND 500", name="body_length"),
        # The notification list, newest first, paged with the same keyset cursor as
        # every other list in the API.
        Index(
            "ix_notifications_recipient_created",
            "recipient_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # The unread badge, which asks only about unread rows. Partial so it stays
        # proportional to what is unread rather than to everything ever received.
        Index(
            "ix_notifications_recipient_unread",
            "recipient_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("read_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Notification {self.id} {self.event_type} read={self.read_at is not None}>"


__all__ = ["Notification"]
