"""ORM models.

Importing this package registers every table on `Base.metadata`, which is what
Alembic autogenerate and the test schema checks rely on.
"""

from app.models.activity import ActivityEvent
from app.models.base import Base
from app.models.comment import Comment
from app.models.idempotency import IdempotencyKey
from app.models.notification import Notification
from app.models.outbox import OutboxEvent
from app.models.session import AuthSession
from app.models.team import Membership, Team
from app.models.user import User
from app.models.work_item import WorkItem

__all__ = [
    "ActivityEvent",
    "AuthSession",
    "Base",
    "Comment",
    "IdempotencyKey",
    "Membership",
    "Notification",
    "OutboxEvent",
    "Team",
    "User",
    "WorkItem",
]
