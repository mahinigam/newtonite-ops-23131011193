"""Notification endpoints.

Everything here is self-scoped: the caller's own notifications, found by
`recipient_id`. There is no team path parameter because a notification is addressed to a
person, not to a team — but the team boundary still holds, enforced by the composite
foreign key on the notification and by the membership cascade that removes notifications
when somebody leaves a team.

Reading somebody else's notification is `404`, not `403`, for the same reason an item in
another team is: an id that is not yours is indistinguishable from an id that was never
issued, so the endpoint cannot be used to discover what exists.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import PrincipalDep, SessionDep
from app.schemas.notifications import (
    MarkAllReadResult,
    NotificationOut,
    NotificationPage,
    UnreadCount,
)
from app.services import notifications
from app.services.cursors import decode_cursor
from app.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, cursor_for

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationPage, summary="The caller's notifications")
async def list_notifications(
    session: SessionDep,
    principal: PrincipalDep,
    cursor: Annotated[str | None, Query(description="`next_cursor` from the previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> NotificationPage:
    """Newest first, one bounded page at a time.

    Paginated independently of any item list: a person's notification history is its own
    sequence, and sharing a cursor with a team's work would make one list's position
    depend on the other's.
    """
    rows, has_more = await notifications.list_notifications(
        session,
        principal=principal,
        cursor=decode_cursor(cursor, id_kind="uuid") if cursor else None,
        limit=limit,
    )
    return NotificationPage(
        items=[NotificationOut.model_validate(row) for row in rows],
        next_cursor=cursor_for(rows, has_more=has_more),
        has_more=has_more,
        limit=limit,
    )


@router.get(
    "/unread-count",
    response_model=UnreadCount,
    summary="How many of the caller's notifications are unread",
)
async def unread_count(session: SessionDep, principal: PrincipalDep) -> UnreadCount:
    """A cheap badge: one count over the unread rows, served by a partial index."""
    return UnreadCount(unread=await notifications.unread_count(session, principal=principal))


@router.post(
    "/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark one notification as read",
)
async def mark_read(
    notification_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> None:
    """Idempotent: marking an already-read notification changes nothing and still succeeds.

    `read_at` records when it was *first* read, so a retry cannot rewrite the timestamp.
    """
    await notifications.mark_read(session, principal=principal, notification_id=notification_id)


@router.post(
    "/read-all",
    response_model=MarkAllReadResult,
    summary="Mark every unread notification as read",
)
async def mark_all_read(session: SessionDep, principal: PrincipalDep) -> MarkAllReadResult:
    """Returns how many rows actually changed, so a client can tell "nothing to do" apart."""
    marked = await notifications.mark_all_read(session, principal=principal)
    return MarkAllReadResult(marked=marked)
