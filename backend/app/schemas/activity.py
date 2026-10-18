"""Activity history response schemas.

The timeline is a read-only view of what happened, so there is no request model here:
history is never written over HTTP, only by the operations it describes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.models.enums import ActivityType
from app.schemas import ResponseModel
from app.schemas.pagination import Page
from app.schemas.work_items import UserRef


class ActivityEventResponse(ResponseModel):
    """One thing that happened.

    `from_value` and `to_value` are the stored values — a status, a priority, a type, or
    a user id for an assignment — and are the fact. `meta` is rendering convenience (a
    display name, a rejection note, a comment id) and a client must work without it.
    """

    id: int
    work_item_id: uuid.UUID
    event_type: ActivityType
    from_value: str | None
    to_value: str | None
    meta: dict[str, Any]
    actor: UserRef
    created_at: datetime
    #: The item's title *as it is now*, joined when a feed shows more than one item.
    #: A timeline for one item does not need it and leaves it null. It is read live rather
    #: than copied into `meta` at write time, so a rename shows through instead of the
    #: history naming a title the item no longer has.
    work_item_title: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> ActivityEventResponse:
        return cls(
            id=row.id,
            work_item_id=row.work_item_id,
            event_type=row.event_type,
            from_value=row.from_value,
            to_value=row.to_value,
            meta=row.meta,
            actor=UserRef(
                id=row.actor_id,
                email=row.actor_email,
                display_name=row.actor_name,
            ),
            created_at=row.created_at,
            work_item_title=getattr(row, "work_item_title", None),
        )


class ActivityPage(Page[ActivityEventResponse]):
    """One page of a timeline, newest first."""


__all__ = ["ActivityEventResponse", "ActivityPage"]
