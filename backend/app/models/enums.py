"""Domain enumerations.

These are the single Python source of truth for the values stored in the
database. The database enforces the same value sets with CHECK constraints
(see `alembic/versions/0001_initial_schema.py`), so an application bug cannot
persist an out-of-range value.

PostgreSQL enum types are deliberately not used: adding a status or priority
during a live review should be an additive migration rather than a type
rewrite.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """A user's role *within a single team*."""

    VIEWER = "viewer"
    MEMBER = "member"
    LEAD = "lead"
    ADMIN = "admin"


class WorkItemType(StrEnum):
    INCIDENT = "incident"
    CUSTOMER_ISSUE = "customer_issue"
    PAYMENT = "payment"
    COMPLIANCE = "compliance"
    TASK = "task"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class Status(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    CLOSED = "closed"


class ActivityType(StrEnum):
    CREATED = "created"
    CLAIMED = "claimed"
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    STATUS_CHANGED = "status_changed"
    APPROVED = "approved"
    REJECTED = "rejected"
    PRIORITY_CHANGED = "priority_changed"
    DUE_DATE_CHANGED = "due_date_changed"
    TYPE_CHANGED = "type_changed"
    TITLE_CHANGED = "title_changed"
    DESCRIPTION_CHANGED = "description_changed"
    COMMENTED = "commented"


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class OutboxEventType(StrEnum):
    """The operational moments that produce a notification.

    Deliberately small. Each one names a moment a person needs to know about, and each
    has exactly one obvious recipient (the item's assignee). Events with no recipient —
    a claim, a comment — are already served by the activity history, so they do not
    produce a notification and never enter the outbox.
    """

    WORK_ITEM_ASSIGNED = "work_item_assigned"
    WORK_ITEM_APPROVED = "work_item_approved"
    WORK_ITEM_REJECTED = "work_item_rejected"


class OutboxStatus(StrEnum):
    """A queued event's delivery state.

    `failed` is terminal and reached only after the attempt limit, so a row in that state
    is a real, visible operational problem rather than one still being retried.
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class Operation(StrEnum):
    """The logical operation a retryable request belongs to.

    Idempotency keys are scoped to `(actor, operation, key)`, so two endpoints can use
    the same key string without colliding, and a key used for one operation cannot
    replay another operation's result.
    """

    CREATE_WORK_ITEM = "create_work_item"
    CREATE_COMMENT = "create_comment"


def values(enum_class: type[StrEnum]) -> tuple[str, ...]:
    """Return the stored string values of an enum, for CHECK constraints."""
    return tuple(member.value for member in enum_class)


def check_in(column: str, enum_class: type[StrEnum]) -> str:
    """Build a SQL `CHECK` expression limiting `column` to an enum's values."""
    joined = ", ".join(f"'{value}'" for value in values(enum_class))
    return f"{column} IN ({joined})"
