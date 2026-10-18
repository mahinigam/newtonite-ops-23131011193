"""Writing and draining the transactional outbox.

This module is deliberately split in two halves that never run in the same transaction:

* `enqueue` is called **inside** the mutation's transaction, by the service that changed
  the item. It only inserts a row. It never commits, exactly like `activity.record`, so
  the event and the change it describes are committed or rolled back together.
* `claim_batch` / `mark_delivered` / `mark_failed` are called by the worker, in its own
  transaction. They own the delivery lifecycle and know nothing about why an event
  exists.

`claim_batch` is the concurrency-sensitive part. `SELECT ... FOR UPDATE SKIP LOCKED` lets
two workers drain the same queue without either blocking on the other and without any
row being delivered twice *by two workers at once*. A row that is already locked is
skipped rather than waited for, so throughput scales with workers instead of collapsing
under contention.

Delivery is at-least-once, by design. The crash case — a worker that dies after writing
the notification but before committing — leaves the row pending, so the next poll tries
again. Exactly-once *effect* comes from the consumer's unique constraint on
`outbox_event_id`, not from pretending the network never fails.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxEvent, Team
from app.models.enums import OutboxEventType, OutboxStatus

#: First retry delay, doubled per attempt.
BACKOFF_BASE_SECONDS = 2

#: Retries stop growing at this delay. Without a cap, the queue would eventually wait
#: hours between attempts and a transient failure would look permanent.
BACKOFF_CAP_SECONDS = 60

#: How many attempts before an event is declared failed. Small: delivery writes one row
#: to a local table, so repeated failure means something is genuinely wrong.
DEFAULT_MAX_ATTEMPTS = 5

#: The stored error is diagnostic, not user-facing. Truncated so one pathological
#: exception cannot bloat the table.
MAX_ERROR_LENGTH = 1000


def backoff_seconds(attempt: int) -> int:
    """Seconds to wait after the `attempt`-th failure (1-based).

    Exponential with a cap: 2, 4, 8, 16, 32, 60, 60 … A pure exponential would make the
    fifth retry wait over half an hour, which is not "retrying", it is "giving up slowly".
    """
    if attempt < 1:
        return 0
    delay = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
    return min(int(delay), BACKOFF_CAP_SECONDS)


async def enqueue(
    session: AsyncSession,
    *,
    event_type: OutboxEventType,
    team_id: uuid.UUID,
    aggregate_id: uuid.UUID,
    payload: dict[str, Any],
    aggregate_type: str = "work_item",
) -> int:
    """Record the intent to notify somebody. **Does not commit.**

    The caller is expected to be inside the transaction that performs the change this
    event describes, so a rollback removes the event too. That is the guarantee the whole
    module exists for: there is no window in which a change committed but its
    notification was never queued.
    """
    event_id = (
        await session.execute(
            pg_insert(OutboxEvent)
            .values(
                event_type=event_type.value,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                team_id=team_id,
                payload=payload,
                status=OutboxStatus.PENDING.value,
            )
            .returning(OutboxEvent.id)
        )
    ).scalar_one()
    return int(event_id)


async def claim_batch(
    session: AsyncSession,
    *,
    limit: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[Any]:
    """Lock up to `limit` due, pending events for this worker.

    Rows are returned with every column the consumer needs. They stay locked until the
    caller commits or rolls back, which is what prevents a second worker from taking them
    meanwhile — and `SKIP LOCKED` means it takes other work instead of waiting.

    A row whose `attempts` already reached the limit is excluded even if it is somehow
    still pending, so a bug in the failure path cannot become an infinite retry loop.
    """
    statement = (
        select(
            OutboxEvent.id,
            OutboxEvent.event_type,
            OutboxEvent.aggregate_type,
            OutboxEvent.aggregate_id,
            OutboxEvent.team_id,
            OutboxEvent.payload,
            OutboxEvent.attempts,
            OutboxEvent.created_at,
        )
        .where(
            OutboxEvent.status == OutboxStatus.PENDING.value,
            OutboxEvent.next_attempt_at <= func.now(),
            OutboxEvent.attempts < max_attempts,
        )
        .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await session.execute(statement)).all())


async def mark_delivered(session: AsyncSession, *, event_id: int) -> None:
    """Record a successful delivery. Does not commit.

    The `status = 'pending'` condition is a second guard on top of the row lock: if
    anything else ever completes this event, this update matches nothing rather than
    overwriting the outcome.
    """
    await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.id == event_id,
            OutboxEvent.status == OutboxStatus.PENDING.value,
        )
        .values(
            status=OutboxStatus.DELIVERED.value,
            processed_at=func.now(),
            last_error=None,
        )
    )


async def mark_failed(
    session: AsyncSession,
    *,
    event_id: int,
    error: str,
    attempts: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """Record a failed attempt. Returns True when this attempt exhausted the retries.

    Before the limit, the event goes back to `pending` with `next_attempt_at` pushed
    forward by the backoff. At the limit it becomes `failed`, which is **terminal and
    visible**: `attempts` and `last_error` stay on the row, and the ops summary counts
    it. A retry that has silently given up is indistinguishable from one still working,
    which is how queues rot unnoticed.

    `next_attempt_at` is computed by PostgreSQL (`now() + interval`), not from the
    application clock, so backoff is consistent across workers with skewed clocks.
    """
    exhausted = attempts >= max_attempts
    values: dict[str, Any] = {
        "attempts": attempts,
        "last_error": error[:MAX_ERROR_LENGTH],
    }
    if exhausted:
        values["status"] = OutboxStatus.FAILED.value
        values["processed_at"] = func.now()
    else:
        values["next_attempt_at"] = func.now() + timedelta(seconds=backoff_seconds(attempts))

    await session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.id == event_id,
            OutboxEvent.status == OutboxStatus.PENDING.value,
        )
        .values(**values)
    )
    return exhausted


async def summarise(session: AsyncSession, *, team_ids: tuple[uuid.UUID, ...]) -> list[Any]:
    """Delivery counts per team and status, for an operator.

    Only counts, never payloads: this is a health signal, not a way to read other
    people's notifications. `failed` is included on purpose — a retry that has silently
    given up is indistinguishable from one still working, which is how queues rot
    unnoticed.
    """
    if not team_ids:
        return []
    statement = (
        select(
            OutboxEvent.team_id,
            Team.key.label("team_key"),
            OutboxEvent.status,
            func.count().label("count"),
        )
        .join(Team, Team.id == OutboxEvent.team_id)
        .where(OutboxEvent.team_id.in_(team_ids))
        .group_by(OutboxEvent.team_id, Team.key, OutboxEvent.status)
    )
    return list((await session.execute(statement)).all())


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "MAX_ERROR_LENGTH",
    "backoff_seconds",
    "claim_batch",
    "enqueue",
    "mark_delivered",
    "mark_failed",
    "summarise",
]
