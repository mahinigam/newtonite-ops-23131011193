"""Reading and writing activity history.

Every operationally meaningful change to a work item goes through `record`, which does
exactly one thing: insert a row into `activity_events` **in the caller's transaction**.

That last detail is the whole point. `record` never commits, so the history row and the
state change it describes commit together or not at all. If the caller rolls back — a lost
version check, a constraint violation, an unexpected error — the history entry disappears
with it, and the timeline cannot claim something that did not happen.

Conventions for the rendered values, shared with the seed data so a reviewer sees one
consistent timeline:

* `from_value` / `to_value` hold the raw stored value: a status, a priority, a type, or a
  user id for assignment changes. They are the fact.
* `meta` holds whatever helps render it — a display name, a rejection note, the id of a
  comment. They are convenience, and clients must work without them.

The same module also **reads** history, because a timeline is one concept whether it is
being appended to or displayed. Two readers are exposed:

* `list_item_activity` — one item's timeline, authorized against the item's own team;
* `list_team_activity` — a team's recent activity, authorized against the team.

They paginate with their own cursor, independently of any item list, because a timeline
is not a page of items and should not share a position with one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.errors import NotFound
from app.models import ActivityEvent, User, WorkItem
from app.models.enums import ActivityType
from app.policy.principal import Principal, require_item_access
from app.policy.roles import Permission
from app.services.cursors import Cursor
from app.services.pagination import DEFAULT_PAGE_SIZE, descending_keyset

# The actor is joined, so a page of events costs one query rather than one per row.
_ACTOR = aliased(User, name="event_actor")

EVENT_QUERY = select(
    ActivityEvent.id,
    ActivityEvent.work_item_id,
    ActivityEvent.team_id,
    ActivityEvent.event_type,
    ActivityEvent.from_value,
    ActivityEvent.to_value,
    ActivityEvent.meta,
    ActivityEvent.created_at,
    _ACTOR.id.label("actor_id"),
    _ACTOR.email.label("actor_email"),
    _ACTOR.display_name.label("actor_name"),
).join(_ACTOR, _ACTOR.id == ActivityEvent.actor_id)


async def record(
    session: AsyncSession,
    *,
    work_item_id: uuid.UUID,
    team_id: uuid.UUID,
    actor_id: uuid.UUID,
    event_type: ActivityType,
    from_value: str | None = None,
    to_value: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Append one history event. Does not commit.

    Callers are expected to be inside a transaction that also contains the change
    this event describes.
    """
    await session.execute(
        insert(ActivityEvent).values(
            work_item_id=work_item_id,
            team_id=team_id,
            actor_id=actor_id,
            event_type=event_type.value,
            from_value=from_value,
            to_value=to_value,
            meta=meta or {},
        )
    )


# --------------------------------------------------------------------------- #
# Reading history
# --------------------------------------------------------------------------- #


def item_activity_statement(
    *,
    work_item_id: uuid.UUID,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> Any:
    """One item's timeline, newest first. Pure, so it can be measured on its own."""
    statement = EVENT_QUERY.where(ActivityEvent.work_item_id == work_item_id)
    if cursor is not None:
        statement = statement.where(
            descending_keyset(ActivityEvent.created_at, ActivityEvent.id, cursor)
        )
    return statement.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc()).limit(
        limit + 1
    )


def team_activity_statement(
    *,
    team_id: uuid.UUID,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    actor_id: uuid.UUID | None = None,
) -> Any:
    """A team's recent activity, newest first.

    Scoped by the denormalised `team_id` on the event, so the feed needs no join back to
    `work_items` to *authorize* — and, more importantly, so a team feed can never be built
    from a query that forgot the team filter. The join that is here exists only to name the
    item on each row: a team feed spans many items, so "assigned this item" is ambiguous
    without it.

    `since` and `until` are inclusive bounds, which is what a date-range control means when
    it says "from" and "to". The cursor is applied on top, so a caller paging a range gets
    the next page *of that range*. `actor_id` narrows to one person's actions; it can only
    remove rows from a set that is already team-scoped, so it cannot widen access.
    """
    statement = (
        EVENT_QUERY.add_columns(WorkItem.title.label("work_item_title"))
        .join(WorkItem, WorkItem.id == ActivityEvent.work_item_id)
        .where(ActivityEvent.team_id == team_id)
    )
    if since is not None:
        statement = statement.where(ActivityEvent.created_at >= since)
    if until is not None:
        statement = statement.where(ActivityEvent.created_at <= until)
    if actor_id is not None:
        statement = statement.where(ActivityEvent.actor_id == actor_id)
    if cursor is not None:
        statement = statement.where(
            descending_keyset(ActivityEvent.created_at, ActivityEvent.id, cursor)
        )
    return statement.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc()).limit(
        limit + 1
    )


async def _page(session: AsyncSession, statement: Any, limit: int) -> tuple[list[Any], bool]:
    rows = list((await session.execute(statement)).all())
    # One extra row answers "is there another page?" without a second count query.
    has_more = len(rows) > limit
    return rows[:limit], has_more


async def _item_team(session: AsyncSession, item_id: uuid.UUID) -> uuid.UUID:
    """The team of the item, or `NotFound`.

    Only the team is selected: the feed needs it to authorize, and loading the whole item
    to answer one question would be wasteful.
    """
    team_id = (
        await session.execute(select(WorkItem.team_id).where(WorkItem.id == item_id))
    ).scalar_one_or_none()
    if team_id is None:
        raise NotFound("Work item not found.")
    return team_id


async def list_item_activity(
    session: AsyncSession,
    *,
    principal: Principal,
    item_id: uuid.UUID,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[Any], bool]:
    """One page of an item's timeline, after authorizing against the item's team."""
    team_id = await _item_team(session, item_id)
    require_item_access(principal, team_id=team_id, permission=Permission.VIEW_TEAM)
    return await _page(
        session, item_activity_statement(work_item_id=item_id, cursor=cursor, limit=limit), limit
    )


async def list_team_activity(
    session: AsyncSession,
    *,
    principal: Principal,
    team_id: uuid.UUID,
    cursor: Cursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    actor_id: uuid.UUID | None = None,
) -> tuple[list[Any], bool]:
    """One page of a team's activity feed.

    Authorized here rather than only by the route's dependency, so the function cannot be
    called on an unauthorized team by some future caller. A team the caller is not in is
    `404`, not `403` — the same answer as a team that does not exist.
    """
    principal.require_permission(team_id, Permission.VIEW_TEAM)
    return await _page(
        session,
        team_activity_statement(
            team_id=team_id,
            cursor=cursor,
            limit=limit,
            since=since,
            until=until,
            actor_id=actor_id,
        ),
        limit,
    )


__all__ = [
    "EVENT_QUERY",
    "item_activity_statement",
    "list_item_activity",
    "list_team_activity",
    "record",
    "team_activity_statement",
]
