"""In-app notifications: the consumer side of the outbox, and the read side.

Two responsibilities, kept together because they are two views of one concept:

* `deliver` takes one claimed outbox event and writes **at most one** notification. It is
  the consumer, so it must be idempotent — the outbox is at-least-once and a worker can
  legitimately process the same event twice after a crash.
* The `list_*` / `mark_*` functions serve the caller's own notifications. They are
  scoped to `recipient_id`, and the composite foreign key on `(team_id, recipient_id)`
  means a notification cannot outlive the membership it belongs to.

Why the consumer can be simple
------------------------------
The hard part — "one notification per event, even under retries" — is a unique
constraint, not application logic. `INSERT ... ON CONFLICT (outbox_event_id) DO NOTHING`
makes the second delivery a no-op, so the reader does not have to reason about ordering,
crashes, or two workers racing. The database decides.

A recipient who is no longer in the team is **not** an error. The notification exists to
tell somebody about work they can act on; once they have left the team there is nobody to
tell, and the delivery is recorded as done rather than retried forever.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFound
from app.models import Membership, Notification
from app.models.enums import OutboxEventType
from app.policy.principal import Principal
from app.services.cursors import Cursor
from app.services.pagination import DEFAULT_PAGE_SIZE, descending_keyset

#: Every column the notification list and the badge need. Selected explicitly, in the
#: same style as the item and activity queries: one query per page, no lazy loads.
NOTIFICATION_QUERY = select(
    Notification.id,
    Notification.team_id,
    Notification.work_item_id,
    Notification.event_type,
    Notification.body,
    Notification.created_at,
    Notification.read_at,
)


def render_body(event_type: str, payload: dict[str, Any]) -> str:
    """Build the sentence a person reads.

    The payload must carry the two facts needed to write it. A payload that cannot be
    rendered is a producer bug, and raising here is deliberate: the worker will retry,
    then mark the event `failed`, which is visible. Silently inventing a body would hide
    the bug behind a plausible-looking notification.
    """
    actor = payload.get("actor_name")
    title = payload.get("work_item_title")
    if not actor or not title:
        raise ValueError(
            f"outbox payload for {event_type} is missing actor_name or work_item_title"
        )

    if event_type == OutboxEventType.WORK_ITEM_ASSIGNED.value:
        return f"{actor} assigned you “{title}”."
    if event_type == OutboxEventType.WORK_ITEM_APPROVED.value:
        return f"{actor} approved “{title}”."
    if event_type == OutboxEventType.WORK_ITEM_REJECTED.value:
        return f"{actor} sent “{title}” back to you for rework."
    raise ValueError(f"unknown outbox event type {event_type!r}")


async def deliver(session: AsyncSession, event: Any) -> bool:
    """Consume one outbox event. Returns True when a notification was created.

    Does not commit: the worker owns the transaction, so a notification and the event's
    `delivered` mark commit together.

    Three outcomes, all of which the caller should treat as success:

    * the recipient is still a member and had not been told yet — one row inserted;
    * the event was already delivered — `ON CONFLICT DO NOTHING`, no second row;
    * the recipient has since left the team — nothing to deliver, nothing to retry.
    """
    recipient_id = event.payload.get("recipient_id")
    if recipient_id is None:
        raise ValueError("outbox payload is missing recipient_id")

    recipient_uuid = uuid.UUID(str(recipient_id))

    still_a_member = (
        await session.execute(
            select(Membership.user_id)
            .where(
                Membership.team_id == event.team_id,
                Membership.user_id == recipient_uuid,
            )
            .limit(1)
        )
    ).first()
    if still_a_member is None:
        return False

    body = render_body(event.event_type, event.payload)
    inserted = (
        await session.execute(
            pg_insert(Notification)
            .values(
                outbox_event_id=event.id,
                recipient_id=recipient_uuid,
                team_id=event.team_id,
                work_item_id=event.aggregate_id,
                event_type=event.event_type,
                body=body,
            )
            .on_conflict_do_nothing(index_elements=["outbox_event_id"])
            .returning(Notification.id)
        )
    ).scalar_one_or_none()
    return inserted is not None


# --------------------------------------------------------------------------- #
# Reading and acknowledging
# --------------------------------------------------------------------------- #


def notification_statement(
    *,
    recipient_id: uuid.UUID,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> Any:
    """One page of a person's notifications, newest first. Pure, so it can be measured."""
    statement = NOTIFICATION_QUERY.where(Notification.recipient_id == recipient_id)
    if cursor is not None:
        statement = statement.where(
            descending_keyset(Notification.created_at, Notification.id, cursor)
        )
    return statement.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(
        limit + 1
    )


async def list_notifications(
    session: AsyncSession,
    *,
    principal: Principal,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[Any], bool]:
    """One page of the caller's own notifications.

    The filter is the caller's id and nothing else. A notification about a team the
    caller has left cannot appear, because the membership cascade removed it — the
    boundary is a foreign key, not a filter somebody could forget.
    """
    rows = list(
        (
            await session.execute(
                notification_statement(recipient_id=principal.user.id, cursor=cursor, limit=limit)
            )
        ).all()
    )
    has_more = len(rows) > limit
    return rows[:limit], has_more


async def unread_count(session: AsyncSession, *, principal: Principal) -> int:
    """How many notifications the caller has not read.

    Served by the partial index on unread rows, so the badge costs a count over the
    unread set rather than over a person's whole history.
    """
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.recipient_id == principal.user.id,
                    Notification.read_at.is_(None),
                )
            )
        ).scalar_one()
    )


async def mark_read(
    session: AsyncSession, *, principal: Principal, notification_id: uuid.UUID
) -> None:
    """Mark one of the caller's notifications as read. Idempotent.

    Somebody else's notification is `404`, not `403`: it is indistinguishable from an id
    that was never issued, so the endpoint cannot be used to discover that another
    person's notification exists.

    Re-marking an already-read notification succeeds without changing its timestamp —
    `read_at` is when it was *first* read, so a retry does not rewrite it.
    """
    row = (
        await session.execute(
            select(Notification.id, Notification.read_at).where(
                Notification.id == notification_id,
                Notification.recipient_id == principal.user.id,
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Notification not found.")

    if row.read_at is None:
        await session.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.recipient_id == principal.user.id,
                Notification.read_at.is_(None),
            )
            .values(read_at=func.now())
        )
    await session.commit()


async def mark_all_read(session: AsyncSession, *, principal: Principal) -> int:
    """Mark every unread notification of the caller as read. Returns how many changed."""
    result = await session.execute(
        update(Notification)
        .where(
            Notification.recipient_id == principal.user.id,
            Notification.read_at.is_(None),
        )
        .values(read_at=func.now())
        .returning(Notification.id)
    )
    marked = len(result.scalars().all())
    await session.commit()
    return marked


__all__ = [
    "NOTIFICATION_QUERY",
    "deliver",
    "list_notifications",
    "mark_all_read",
    "mark_read",
    "notification_statement",
    "render_body",
    "unread_count",
]
