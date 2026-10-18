"""Notification request and response schemas.

Read-only apart from the two acknowledgement bodies: a client never creates a
notification. That is the worker's job, and letting a request body influence it would
make the outbox meaningless.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.enums import OutboxEventType
from app.schemas import ResponseModel
from app.schemas.pagination import Page


class NotificationOut(ResponseModel):
    """One notification, addressed to the caller.

    `team_id` and `work_item_id` are enough to build a link: the client already has the
    caller's memberships, so it can resolve the team key without a second request.
    `body` is rendering-ready and records what the person was told, even if the item has
    since been renamed.
    """

    id: uuid.UUID
    team_id: uuid.UUID
    work_item_id: uuid.UUID
    event_type: OutboxEventType
    body: str
    created_at: datetime
    read_at: datetime | None


class NotificationPage(Page[NotificationOut]):
    """One page of the caller's notifications, newest first."""


class UnreadCount(ResponseModel):
    unread: int


class MarkAllReadResult(ResponseModel):
    marked: int


__all__ = ["MarkAllReadResult", "NotificationOut", "NotificationPage", "UnreadCount"]
