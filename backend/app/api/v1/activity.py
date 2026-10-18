"""Activity history endpoints.

Two views of the same append-only table:

* `/items/{item_id}/activity` — one item's timeline, authorized against the item's team;
* `/teams/{team_id}/activity` — a team's recent activity, authorized against the team.

Both are **read-only by construction**: there is no route that writes an event, and the
database trigger behind `activity_events` refuses `UPDATE` and `DELETE` outright. History
is produced by the operations it describes, never by a client.

Each view paginates with its own cursor, independently of any item list — a timeline is
not a page of items, and the two must not share a position.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import PrincipalDep, SessionDep
from app.errors import ValidationFailed
from app.policy.principal import Principal
from app.schemas.activity import ActivityEventResponse, ActivityPage
from app.services import activity
from app.services.cursors import decode_cursor
from app.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, cursor_for

router = APIRouter(tags=["activity"])


def _aware_bound(value: dt.datetime | None, name: str) -> dt.datetime | None:
    """Reject naive datetimes: their instant depends on the server's time zone.

    A bound without an offset is not a smaller question than one with it — it is an
    ambiguous one, and silently assuming UTC turns "17 Oct in Berlin" into the wrong
    window. The client always knows its offset, so it is required to say it.
    """
    if value is not None and value.tzinfo is None:
        raise ValidationFailed(
            f"`{name}` must include a time-zone offset, for example 2026-10-17T00:00:00Z."
        )
    return value


def _parse_actor(value: str | None, principal: Principal) -> uuid.UUID | None:
    """Turn `actor=me|<id>` into a user id, or 422.

    The filter can only narrow a set that is already scoped to the team, so a valid id for
    somebody outside the team returns an empty page rather than an error — and never
    another team's events.
    """
    if value is None or value == "":
        return None
    if value == "me":
        return principal.user.id
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationFailed("The actor filter must be 'me' or a user id.") from exc


@router.get(
    "/items/{item_id}/activity",
    response_model=ActivityPage,
    summary="Read one work item's history, newest first",
)
async def item_activity(
    item_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    cursor: Annotated[str | None, Query(description="`next_cursor` from the previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ActivityPage:
    """The item's timeline: what happened, who did it, and what changed.

    Newest first, bounded by `limit`, and paged with a cursor that is independent of the
    item list's. An item in a team the caller does not belong to is `404`.
    """
    rows, has_more = await activity.list_item_activity(
        session,
        principal=principal,
        item_id=item_id,
        cursor=decode_cursor(cursor, id_kind="int") if cursor else None,
        limit=limit,
    )

    return ActivityPage(
        items=[ActivityEventResponse.from_row(row) for row in rows],
        next_cursor=cursor_for(rows, has_more=has_more),
        has_more=has_more,
        limit=limit,
    )


@router.get(
    "/teams/{team_id}/activity",
    response_model=ActivityPage,
    summary="Read a team's recent activity, newest first",
)
async def team_activity(
    team_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    cursor: Annotated[str | None, Query(description="`next_cursor` from the previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    since: Annotated[
        dt.datetime | None,
        Query(description="Inclusive lower bound, ISO 8601 with an offset"),
    ] = None,
    until: Annotated[
        dt.datetime | None,
        Query(description="Inclusive upper bound, ISO 8601 with an offset"),
    ] = None,
    actor: Annotated[
        str | None,
        Query(description="`me` or a user id: only that person's actions"),
    ] = None,
) -> ActivityPage:
    """Everything that has happened in one team, newest first.

    Built from the event's denormalised `team_id`, so the feed cannot be assembled from a
    query that forgot the team filter. The item's *current* title is joined in, because a
    team feed spans many items and "assigned this item" does not say which one. A team the
    caller is not a member of is `404`, not `403`.

    `since` and `until` are inclusive; both must carry a time-zone offset, and `since`
    later than `until` is `422`. The cursor applies on top of the bounds, so paging a range
    walks that range.
    """
    since = _aware_bound(since, "since")
    until = _aware_bound(until, "until")
    if since is not None and until is not None and since > until:
        raise ValidationFailed("`since` must not be later than `until`.")

    rows, has_more = await activity.list_team_activity(
        session,
        principal=principal,
        team_id=team_id,
        cursor=decode_cursor(cursor, id_kind="int") if cursor else None,
        limit=limit,
        since=since,
        until=until,
        actor_id=_parse_actor(actor, principal),
    )

    return ActivityPage(
        items=[ActivityEventResponse.from_row(row) for row in rows],
        next_cursor=cursor_for(rows, has_more=has_more),
        has_more=has_more,
        limit=limit,
    )
