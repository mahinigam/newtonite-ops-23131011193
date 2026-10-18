"""Work items: the operations that carry the correctness guarantees.

Four ideas are load-bearing, and each is implemented as a *single conditional SQL
statement* rather than a read followed by a write. That distinction is the whole
difference between behaviour that is correct and behaviour that is usually correct:

1. **Claiming is atomic.** `UPDATE ... WHERE assignee_id IS NULL` — the database decides
   the winner. Two simultaneous claims cannot both see a free item, because neither reads
   before writing.
2. **Stale writes are rejected.** `UPDATE ... WHERE version = :expected` — the comparison
   and the `version = version + 1` bump are one statement, so a concurrent writer cannot
   slip between them. Zero rows affected means somebody else got there first.
3. **Creation is idempotent.** The key row and the item are written in one transaction. A
   duplicate request blocks on the unique index until the first commits, then replays the
   first result instead of creating a second item. A reused key with a different body is
   refused rather than answered with an unrelated item.
4. **History is written in the same transaction as the change.** `activity.record` never
   commits, so if the state change rolls back the history entry does too.

Every mutation also bumps `version` exactly once, which is what makes `ETag` mean
something: version `n` is a specific state, not a count of attempts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import cast, func, insert, literal_column, select, update
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.errors import (
    AlreadyClaimed,
    AssigneeRequired,
    Conflict,
    InvalidAssignee,
    InvalidTransition,
    NotFound,
    StaleVersion,
)
from app.models import Membership, User, WorkItem
from app.models.enums import (
    ActivityType,
    Operation,
    OutboxEventType,
    Priority,
    Status,
    WorkItemType,
)
from app.policy.principal import Principal, require_item_access
from app.policy.roles import Permission, require_permission
from app.policy.workflow import available_transitions, rule_for
from app.schemas.work_items import CreateWorkItemRequest, UpdateWorkItemRequest
from app.services import activity, idempotency, outbox
from app.services.cursors import Cursor
from app.services.pagination import DEFAULT_PAGE_SIZE, descending_keyset

# The text-search configuration, written as a literal rather than a bound parameter.
# It is a fixed internal constant, never user input, and keeping it constant is what lets
# PostgreSQL evaluate the tsquery at plan time — which is what the GIN index on
# `search_vector` requires. A parameterised configuration cannot be folded at plan time.
SEARCH_CONFIG = cast(literal_column("'english'"), REGCONFIG)

# The two joins are built once: a list of 25 items must not cost 51 queries.
_ASSIGNEE = aliased(User, name="assignee")
_CREATOR = aliased(User, name="creator")

ITEM_QUERY = (
    select(
        WorkItem.id,
        WorkItem.team_id,
        WorkItem.title,
        WorkItem.description,
        WorkItem.type,
        WorkItem.priority,
        WorkItem.status,
        WorkItem.version,
        WorkItem.due_at,
        WorkItem.created_at,
        WorkItem.updated_at,
        _ASSIGNEE.id.label("assignee_id"),
        _ASSIGNEE.email.label("assignee_email"),
        _ASSIGNEE.display_name.label("assignee_name"),
        _CREATOR.id.label("creator_id"),
        _CREATOR.email.label("creator_email"),
        _CREATOR.display_name.label("creator_name"),
    )
    .outerjoin(_ASSIGNEE, _ASSIGNEE.id == WorkItem.assignee_id)
    .join(_CREATOR, _CREATOR.id == WorkItem.created_by)
)


@dataclass(frozen=True, slots=True)
class CreateOutcome:
    item: Any
    #: True when this response is the replay of an earlier identical request.
    replayed: bool


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


async def get_item(session: AsyncSession, item_id: uuid.UUID) -> Any | None:
    return (await session.execute(ITEM_QUERY.where(WorkItem.id == item_id))).one_or_none()


async def load_visible_item(
    session: AsyncSession, *, principal: Principal, item_id: uuid.UUID, permission: Permission
) -> tuple[Any, Any]:
    """Fetch an item and authorize the caller against **the item's own team**.

    Returns the row and the caller's role in that team.

    A caller who is not in the item's team is told the item does not exist — the same
    answer as for an id that was never issued. Returning `403` would confirm that
    another team's item exists, which a non-member has no business learning. `403` is
    reserved for a caller who *is* in the team but whose role is insufficient.

    The decision itself lives in `require_item_access`, so this and the activity feed
    cannot answer the same case differently.
    """
    row = await get_item(session, item_id)
    if row is None:
        raise NotFound("Work item not found.")

    role = require_item_access(principal, team_id=row.team_id, permission=permission)
    return row, role


async def list_items(session: AsyncSession, **kwargs: Any) -> tuple[list[Any], bool]:
    """One page of a team's items, newest first.

    Filtering, searching and ordering all happen in PostgreSQL; the caller's page size is
    bounded, so a single request cannot ask for the whole table. Returns the rows and
    whether more remain.

    The statement is built by `list_statement`, which is a pure function — so the query
    the API runs is exactly the query the index measurements in `docs/DATA_MODEL.md`
    describe, rather than a hand-written lookalike.
    """
    limit: int = kwargs.get("limit", DEFAULT_PAGE_SIZE)
    rows = list((await session.execute(list_statement(**kwargs))).all())

    # One extra row is fetched purely to answer "is there another page?" without a
    # second count query.
    has_more = len(rows) > limit
    return rows[:limit], has_more


def _apply_filters(
    statement: Any,
    *,
    statuses: tuple[Status, ...],
    priorities: tuple[Priority, ...],
    types: tuple[WorkItemType, ...],
    search: str | None,
) -> Any:
    """Apply the filters both list queries share.

    Extracted so the team list and the cross-team "assigned to me" list cannot drift into
    slightly different meanings of the same query parameter — the kind of difference that
    shows up as "the filter works on one page but not the other".
    """
    if statuses:
        statement = statement.where(WorkItem.status.in_([status.value for status in statuses]))
    if priorities:
        statement = statement.where(
            WorkItem.priority.in_([priority.value for priority in priorities])
        )
    if types:
        statement = statement.where(WorkItem.type.in_([item_type.value for item_type in types]))
    if search:
        # `websearch_to_tsquery` accepts what a person actually types (quotes, `or`,
        # a leading `-`) and never raises on malformed input, unlike `to_tsquery`.
        statement = statement.where(
            WorkItem.search_vector.op("@@")(func.websearch_to_tsquery(SEARCH_CONFIG, search))
        )
    return statement


def _apply_page(statement: Any, *, cursor: Cursor | None, limit: int) -> Any:
    """Order newest-first and take one extra row, so "is there more?" needs no count."""
    if cursor is not None:
        statement = statement.where(descending_keyset(WorkItem.created_at, WorkItem.id, cursor))
    return statement.order_by(WorkItem.created_at.desc(), WorkItem.id.desc()).limit(limit + 1)


async def list_my_items(
    session: AsyncSession,
    *,
    principal: Principal,
    statuses: tuple[Status, ...] = (),
    priorities: tuple[Priority, ...] = (),
    types: tuple[WorkItemType, ...] = (),
    search: str | None = None,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[Any], bool]:
    """One page of the caller's work, across every team they belong to.

    The team scope is `principal.team_ids`: the memberships loaded from the database for
    this request. Nothing in the query comes from the client except the filters and the
    cursor, so there is no team id to forge.

    A caller with no memberships gets an empty page rather than a query with
    `team_id IN ()` — an empty `IN` is always false, but skipping the round trip keeps
    the no-teams case obviously correct.
    """
    team_ids = principal.team_ids
    if not team_ids:
        return [], False

    rows = list(
        (
            await session.execute(
                my_items_statement(
                    viewer_id=principal.user.id,
                    team_ids=team_ids,
                    statuses=statuses,
                    priorities=priorities,
                    types=types,
                    search=search,
                    cursor=cursor,
                    limit=limit,
                )
            )
        ).all()
    )
    has_more = len(rows) > limit
    return rows[:limit], has_more


def list_statement(
    *,
    team_id: uuid.UUID,
    viewer_id: uuid.UUID,
    statuses: tuple[Status, ...] = (),
    priorities: tuple[Priority, ...] = (),
    types: tuple[WorkItemType, ...] = (),
    assignee_id: uuid.UUID | None = None,
    unassigned: bool = False,
    only_mine: bool = False,
    search: str | None = None,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> Any:
    """Build the team list query. Pure, so it can be compiled and measured on its own.

    Annotated `Any` for the same reason as the other query builders in this module:
    SQLAlchemy's `Select` is generic over its row shape, and the row shape here is a
    long variadic tuple that would obscure rather than document.
    """
    statement = ITEM_QUERY.where(WorkItem.team_id == team_id)

    if unassigned:
        statement = statement.where(WorkItem.assignee_id.is_(None))
    elif only_mine:
        statement = statement.where(WorkItem.assignee_id == viewer_id)
    elif assignee_id is not None:
        statement = statement.where(WorkItem.assignee_id == assignee_id)

    return _apply_page(
        _apply_filters(
            statement, statuses=statuses, priorities=priorities, types=types, search=search
        ),
        cursor=cursor,
        limit=limit,
    )


def my_items_statement(
    *,
    viewer_id: uuid.UUID,
    team_ids: tuple[uuid.UUID, ...],
    statuses: tuple[Status, ...] = (),
    priorities: tuple[Priority, ...] = (),
    types: tuple[WorkItemType, ...] = (),
    search: str | None = None,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> Any:
    """Build the cross-team "assigned to me" query. Pure, so it can be measured alone.

    Two conditions define it, and neither comes from the request: the assignee is the
    caller, and the team is one the caller belongs to. The team list is not a filter the
    client supplies — it is the caller's own membership set, loaded from the database on
    this request. A request cannot widen it.

    This is the query `ix_work_items_assignee_created` was added for: the assignee index
    finds the rows, and the team ids restrict them to teams the caller can see.
    """
    statement = ITEM_QUERY.where(
        WorkItem.assignee_id == viewer_id,
        WorkItem.team_id.in_(list(team_ids)),
    )
    return _apply_page(
        _apply_filters(
            statement, statuses=statuses, priorities=priorities, types=types, search=search
        ),
        cursor=cursor,
        limit=limit,
    )


# --------------------------------------------------------------------------- #
# Creation, with idempotency
# --------------------------------------------------------------------------- #


async def create_item(
    session: AsyncSession,
    *,
    principal: Principal,
    team_id: uuid.UUID,
    request: CreateWorkItemRequest,
    idempotency_key: str,
) -> CreateOutcome:
    """Create a work item exactly once, even if the request arrives twice.

    The `Idempotency-Key` row and the item are written in the same transaction. A
    concurrent duplicate blocks on the unique index `(actor_id, operation, key)` until
    this transaction commits, and then replays this result instead of creating a second
    item.
    """
    role = principal.require_permission(team_id, Permission.CREATE_ITEM)
    digest = idempotency.fingerprint(request)

    key_id = await idempotency.claim_key(
        session,
        actor_id=principal.user.id,
        operation=Operation.CREATE_WORK_ITEM,
        key=idempotency_key,
        fingerprint=digest,
    )

    if key_id is None:
        resource_id = await idempotency.replay(
            session,
            actor_id=principal.user.id,
            operation=Operation.CREATE_WORK_ITEM,
            key=idempotency_key,
            fingerprint=digest,
        )
        item = await get_item(session, resource_id)
        if item is None:  # pragma: no cover - items are never deleted
            raise NotFound("Work item not found.")
        return CreateOutcome(item=item, replayed=True)

    assignee_id = request.assignee_id
    if assignee_id is not None:
        # Passing an item straight to somebody else is an assignment, which a member
        # may not do; taking the work yourself is a claim, which they may.
        if assignee_id != principal.user.id:
            require_permission(role, Permission.ASSIGN_OTHERS)
        await _ensure_assignable(session, team_id=team_id, user_id=assignee_id)

    item_id = (
        await session.execute(
            insert(WorkItem)
            .values(
                team_id=team_id,
                title=request.title,
                description=request.description,
                type=request.type.value,
                priority=request.priority.value,
                status=Status.OPEN.value,
                assignee_id=assignee_id,
                # Never taken from the request: the server decides who created it.
                created_by=principal.user.id,
                due_at=request.due_at,
            )
            .returning(WorkItem.id)
        )
    ).scalar_one()

    await activity.record(
        session,
        work_item_id=item_id,
        team_id=team_id,
        actor_id=principal.user.id,
        event_type=ActivityType.CREATED,
        to_value=Status.OPEN.value,
        meta={"title": request.title},
    )
    if assignee_id is not None:
        await activity.record(
            session,
            work_item_id=item_id,
            team_id=team_id,
            actor_id=principal.user.id,
            event_type=(
                ActivityType.CLAIMED if assignee_id == principal.user.id else ActivityType.ASSIGNED
            ),
            from_value=None,
            to_value=str(assignee_id),
            meta=await _assignee_meta(session, assignee_id),
        )
        await _queue_assignee_event(
            session,
            event_type=OutboxEventType.WORK_ITEM_ASSIGNED,
            item_id=item_id,
            team_id=team_id,
            recipient_id=assignee_id,
            actor=principal,
            title=request.title,
        )

    await idempotency.complete_key(
        session,
        key_id=key_id,
        resource_id=item_id,
        response_status=201,
        response_body={"work_item_id": str(item_id)},
    )
    await session.commit()

    row = await get_item(session, item_id)
    return CreateOutcome(item=row, replayed=False)


async def _ensure_assignable(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Refuse an assignee who is not a member of the team.

    The composite foreign key would refuse it too, but as a raw integrity error. A
    clear 422 is the difference between an actionable message and a database dump.
    """
    exists = (
        await session.execute(
            select(func.count())
            .select_from(Membership)
            .where(Membership.team_id == team_id, Membership.user_id == user_id)
        )
    ).scalar_one()
    if not exists:
        raise InvalidAssignee()


async def _assignee_meta(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """Rendering help for history: the name as it was at the time.

    The id in `to_value` stays authoritative; this is a convenience for the timeline.
    """
    name = (
        await session.execute(select(User.display_name).where(User.id == user_id))
    ).scalar_one_or_none()
    return {"assignee_id": str(user_id), "assignee_name": name}


async def _queue_assignee_event(
    session: AsyncSession,
    *,
    event_type: OutboxEventType,
    item_id: uuid.UUID,
    team_id: uuid.UUID,
    recipient_id: uuid.UUID | None,
    actor: Principal,
    title: str,
) -> None:
    """Queue a notification for the item's assignee. Does not commit.

    Called inside the mutation's transaction, so the event commits with the change or not
    at all — the outbox row can never survive a rolled-back assignment, and an assignment
    can never commit without its notification being queued.

    Two cases produce nothing, deliberately:

    * there is no assignee, so there is nobody to tell;
    * the actor is the assignee. Telling somebody about their own action is noise, and a
      notification about "you assigned yourself" is the kind of thing that teaches people
      to ignore the badge.
    """
    if recipient_id is None or recipient_id == actor.user.id:
        return

    await outbox.enqueue(
        session,
        event_type=event_type,
        team_id=team_id,
        aggregate_id=item_id,
        payload={
            "recipient_id": str(recipient_id),
            "actor_id": str(actor.user.id),
            "actor_name": actor.user.display_name,
            "work_item_title": title,
        },
    )


# --------------------------------------------------------------------------- #
# Claiming
# --------------------------------------------------------------------------- #


async def claim_item(session: AsyncSession, *, principal: Principal, item_id: uuid.UUID) -> Any:
    """Take ownership of an unassigned open item.

    Atomic by construction: the eligibility check and the assignment are the same
    `UPDATE`. There is no window between "is it free?" and "it is mine", so two
    concurrent claims cannot both succeed — the second statement matches no row.

    Claiming does **not** start the work. Starting is a separate, explicit transition,
    so the assignment and the status change are each recorded and each reversible
    independently.

    Claiming an item you already own is a no-op that returns the current state, which
    makes a double-click harmless.
    """
    row, _ = await load_visible_item(
        session, principal=principal, item_id=item_id, permission=Permission.CLAIM_ITEM
    )

    if row.assignee_id == principal.user.id:
        return row

    updated = (
        await session.execute(
            update(WorkItem)
            .where(
                WorkItem.id == item_id,
                # The race is decided here, by the database.
                WorkItem.assignee_id.is_(None),
                WorkItem.status == Status.OPEN.value,
            )
            .values(
                assignee_id=principal.user.id,
                version=WorkItem.version + 1,
                updated_at=func.now(),
            )
            .returning(WorkItem.id)
        )
    ).scalar_one_or_none()

    if updated is None:
        raise await _why_claim_failed(session, item_id=item_id)

    await activity.record(
        session,
        work_item_id=item_id,
        team_id=row.team_id,
        actor_id=principal.user.id,
        event_type=ActivityType.CLAIMED,
        from_value=None,
        to_value=str(principal.user.id),
        meta={"assignee_id": str(principal.user.id), "assignee_name": principal.user.display_name},
    )
    await session.commit()

    fresh = await get_item(session, item_id)
    assert fresh is not None  # the row was just updated in this transaction
    return fresh


async def _why_claim_failed(session: AsyncSession, *, item_id: uuid.UUID) -> Exception:
    """Explain a claim that matched no row.

    Purely diagnostic: the failed `UPDATE` has already guaranteed that this request did
    not claim anything. This only chooses the most useful message, and re-reading
    cannot reintroduce a race because the decision was already made.
    """
    current = await get_item(session, item_id)
    if current is None:  # pragma: no cover - deleted mid-request is impossible today
        return NotFound("Work item not found.")
    if current.assignee_id is not None:
        return AlreadyClaimed()
    if current.status != Status.OPEN.value:
        return InvalidTransition(f"An item with status '{current.status}' cannot be claimed.")
    return Conflict("This item could not be claimed.")


# --------------------------------------------------------------------------- #
# Updating, with a version precondition
# --------------------------------------------------------------------------- #

# Which field maps to which history event, and to which stored value.
_FIELD_EVENTS: dict[str, tuple[ActivityType, bool]] = {
    # field -> (event, is_enum)
    "title": (ActivityType.TITLE_CHANGED, False),
    "description": (ActivityType.DESCRIPTION_CHANGED, False),
    "type": (ActivityType.TYPE_CHANGED, True),
    "priority": (ActivityType.PRIORITY_CHANGED, True),
    "due_at": (ActivityType.DUE_DATE_CHANGED, False),
}


def _comparable(value: Any, *, is_enum: bool) -> Any:
    """Normalise a value the way it is stored, for change detection."""
    if value is None:
        return None
    if is_enum:
        return value.value if hasattr(value, "value") else value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def update_item(
    session: AsyncSession,
    *,
    principal: Principal,
    item_id: uuid.UUID,
    request: UpdateWorkItemRequest,
    expected_version: int,
) -> Any:
    """Apply an edit, but only to the version the caller has actually seen.

    Absent fields are left alone; a field explicitly sent as `null` clears it. That
    distinction comes from Pydantic's `model_fields_set`, which is why a PATCH can
    express "no opinion" and "make this empty" separately.

    A request that changes nothing returns the current item without bumping the version
    or writing history: a no-op is not an event.
    """
    row, role = await load_visible_item(
        session, principal=principal, item_id=item_id, permission=Permission.EDIT_ITEM
    )

    provided = request.model_fields_set
    changes: list[tuple[ActivityType, str | None, str | None, dict[str, Any]]] = []
    values: dict[str, Any] = {}
    #: Set when this edit gives the item to somebody new, so a notification is queued
    #: after the history rows and before the commit.
    notified_assignee: uuid.UUID | None = None

    for field, (event, is_enum) in _FIELD_EVENTS.items():
        if field not in provided:
            continue
        new_value = getattr(request, field)
        before = _comparable(getattr(row, field), is_enum=is_enum)
        after = _comparable(new_value, is_enum=is_enum)
        if before == after:
            continue
        values[field] = new_value.value if is_enum and new_value is not None else new_value
        changes.append((event, _render(before), _render(after), {}))

    if "assignee_id" in provided:
        new_assignee = request.assignee_id
        if new_assignee != row.assignee_id:
            _require_assignee_change_allowed(
                role=role, principal=principal, current=row.assignee_id, target=new_assignee
            )
            if new_assignee is not None:
                await _ensure_assignable(session, team_id=row.team_id, user_id=new_assignee)
            values["assignee_id"] = new_assignee
            notified_assignee = new_assignee
            if new_assignee is None:
                changes.append((ActivityType.UNASSIGNED, _render(row.assignee_name), None, {}))
            elif row.assignee_id is None:
                changes.append(
                    (
                        ActivityType.CLAIMED
                        if new_assignee == principal.user.id
                        else ActivityType.ASSIGNED,
                        None,
                        str(new_assignee),
                        await _assignee_meta(session, new_assignee),
                    )
                )
            else:
                changes.append(
                    (
                        ActivityType.ASSIGNED,
                        _render(row.assignee_name),
                        str(new_assignee),
                        await _assignee_meta(session, new_assignee),
                    )
                )

    if not values:
        return row

    updated = (
        await session.execute(
            update(WorkItem)
            .where(
                WorkItem.id == item_id,
                # The precondition and the bump in one statement: no other writer can
                # interleave between the check and the write.
                WorkItem.version == expected_version,
            )
            .values(**values, version=WorkItem.version + 1, updated_at=func.now())
            .returning(WorkItem.version)
        )
    ).scalar_one_or_none()

    if updated is None:
        current = await get_item(session, item_id)
        if current is None:  # pragma: no cover - items are never deleted
            raise NotFound("Work item not found.")
        raise StaleVersion(details={"current": _as_reconciliation(current)})

    for event, before, after, meta in changes:
        await activity.record(
            session,
            work_item_id=item_id,
            team_id=row.team_id,
            actor_id=principal.user.id,
            event_type=event,
            from_value=before,
            to_value=after,
            meta=meta,
        )

    if notified_assignee is not None:
        await _queue_assignee_event(
            session,
            event_type=OutboxEventType.WORK_ITEM_ASSIGNED,
            item_id=item_id,
            team_id=row.team_id,
            recipient_id=notified_assignee,
            actor=principal,
            title=values.get("title", row.title),
        )

    await session.commit()

    fresh = await get_item(session, item_id)
    assert fresh is not None
    return fresh


def _require_assignee_change_allowed(
    *,
    role: Any,
    principal: Principal,
    current: uuid.UUID | None,
    target: uuid.UUID | None,
) -> None:
    """Decide whether an assignment change is a claim or a reassignment.

    Taking an unowned item is a claim, and unassigning yourself undoes one. Anything
    that moves work to or from *somebody else* is an assignment, which needs the
    assignment permission — so a member cannot quietly take a colleague's item, and
    cannot give away work they do not own.
    """
    me = principal.user.id
    others_involved = (current not in (None, me)) or (target not in (None, me))
    require_permission(role, Permission.ASSIGN_OTHERS if others_involved else Permission.CLAIM_ITEM)


def _render(value: Any) -> str | None:
    """History stores text; `None` stays `None` rather than becoming the string 'None'."""
    return None if value is None else str(value)


def _as_reconciliation(row: Any) -> dict[str, Any]:
    """The smallest useful payload for a client that lost a version race.

    Just enough to see what it now is and to retry with the correct precondition,
    without duplicating the full representation inside an error body.
    """
    return {
        "id": str(row.id),
        "version": row.version,
        "status": row.status,
        "priority": row.priority,
        "assignee_id": str(row.assignee_id) if row.assignee_id else None,
        "updated_at": row.updated_at.isoformat(),
    }


# --------------------------------------------------------------------------- #
# Workflow transitions
# --------------------------------------------------------------------------- #


async def transition_item(
    session: AsyncSession,
    *,
    principal: Principal,
    item_id: uuid.UUID,
    target: Status,
    expected_version: int,
    note: str | None = None,
) -> Any:
    """Move an item through the workflow, if the workflow and the role allow it.

    Three separate things must hold, and all three are checked on the server:

    * the transition must exist in the state machine;
    * the caller's role must grant the permission that transition requires;
    * the caller's version must be current.

    A transition that cannot start (for example starting unassigned work) is refused
    with a specific reason rather than a generic conflict.
    """
    row, role = await load_visible_item(
        session, principal=principal, item_id=item_id, permission=Permission.VIEW_TEAM
    )

    current = Status(row.status)
    rule = rule_for(current, target)
    if rule is None:
        raise InvalidTransition(
            f"An item with status '{current.value}' cannot move to '{target.value}'.",
            details={
                "current_status": current.value,
                "available_transitions": [
                    status.value
                    for status in available_transitions(
                        status=current,
                        role=role,
                        has_assignee=row.assignee_id is not None,
                    )
                ],
            },
        )

    require_permission(role, rule.permission)

    if rule.requires_assignee and row.assignee_id is None:
        raise AssigneeRequired()

    updated = (
        await session.execute(
            update(WorkItem)
            .where(
                WorkItem.id == item_id,
                WorkItem.version == expected_version,
                # The status is part of the condition as well: a transition decided
                # from a status that has since changed must not apply.
                WorkItem.status == current.value,
            )
            .values(status=target.value, version=WorkItem.version + 1, updated_at=func.now())
            .returning(WorkItem.version)
        )
    ).scalar_one_or_none()

    if updated is None:
        fresh = await get_item(session, item_id)
        if fresh is None:  # pragma: no cover
            raise NotFound("Work item not found.")
        if fresh.version != expected_version:
            raise StaleVersion(details={"current": _as_reconciliation(fresh)})
        if fresh.status != current.value:
            raise InvalidTransition(
                f"Somebody moved this item to '{fresh.status}' in the meantime.",
                details={"current_status": fresh.status},
            )
        raise Conflict("The transition could not be applied.")

    await activity.record(
        session,
        work_item_id=item_id,
        team_id=row.team_id,
        actor_id=principal.user.id,
        event_type=rule.event,
        from_value=current.value,
        to_value=target.value,
        meta=({"note": note} if note else {}),
    )

    # Approval and rejection are the two transitions whose outcome the person doing the
    # work needs to hear about. Every other move is visible in the item and the feed.
    if rule.event is ActivityType.APPROVED:
        await _queue_assignee_event(
            session,
            event_type=OutboxEventType.WORK_ITEM_APPROVED,
            item_id=item_id,
            team_id=row.team_id,
            recipient_id=row.assignee_id,
            actor=principal,
            title=row.title,
        )
    elif rule.event is ActivityType.REJECTED:
        await _queue_assignee_event(
            session,
            event_type=OutboxEventType.WORK_ITEM_REJECTED,
            item_id=item_id,
            team_id=row.team_id,
            recipient_id=row.assignee_id,
            actor=principal,
            title=row.title,
        )

    await session.commit()

    result = await get_item(session, item_id)
    assert result is not None
    return result


__all__ = [
    "CreateOutcome",
    "claim_item",
    "create_item",
    "get_item",
    "list_items",
    "list_my_items",
    "list_statement",
    "load_visible_item",
    "my_items_statement",
    "transition_item",
    "update_item",
]
