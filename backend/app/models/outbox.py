"""The transactional outbox.

Why this table exists
---------------------
Some consequences of a mutation are *not* part of the request's own transaction: telling
somebody they were assigned work is a separate, fallible delivery. Doing that inline would
make the mutation depend on a channel that may be slow or down — and doing it *after*
committing would be worse, because a crash between the two would lose the notification
with nothing to retry.

So the mutation writes a row here **in its own transaction**. Either the assignment and
the intent to notify both commit, or neither does. A separate worker delivers the rows
afterwards, with bounded retries.

What this buys, stated precisely
--------------------------------
* A committed assignment always has a matching outbox row, because they commit together.
* A rolled-back assignment leaves no outbox row, because they roll back together.
* A worker that crashes mid-delivery rolls back, leaving the row pending; the next poll
  picks it up again. Duplicate delivery is harmless because the consumer is idempotent
  on `(outbox_event_id)` (see `app.models.notification`).

The row is the durable intent. Delivery is at-least-once, and the notification table's
unique constraint turns that into exactly-once *effect*.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import OutboxEventType, OutboxStatus, check_in


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    # Monotonic, like the activity log: a strict order for the worker to drain in, and a
    # natural "oldest first" that does not depend on clock precision.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'work_item'")
    )
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Denormalised from the aggregate so an operator's summary can be scoped to the teams
    # they administer without joining back to `work_items`, and so a query that forgot
    # the team filter is conspicuous.
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )

    # Everything the consumer needs to render the notification. Kept as JSONB rather than
    # a widening set of columns: the payload is the producer's business, and adding a
    # field should not be a migration.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # When the next attempt is due. Set to `now()` on insert, and pushed forward by the
    # backoff after a failure, so a retry storm cannot busy-loop the worker.
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(check_in("event_type", OutboxEventType), name="event_type_is_known"),
        CheckConstraint(check_in("status", OutboxStatus), name="status_is_known"),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        # The worker's claim query, exactly: pending rows, oldest due first. Partial so
        # the index stays proportional to the backlog rather than to everything ever
        # delivered — a queue index that grows forever stops being an index.
        Index(
            "ix_outbox_events_pending",
            "next_attempt_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OutboxEvent {self.id} {self.event_type} {self.status}>"


__all__ = ["OutboxEvent"]
