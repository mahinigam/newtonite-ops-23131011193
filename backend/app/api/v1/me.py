"""Views scoped to the authenticated person rather than to one team.

`GET /api/v1/me/items` is the "assigned to me" view across **every** team the caller
belongs to. It exists because the operational question a person actually asks is "what is
on my plate?", not "what is on my plate in this one team?" — and answering it by making
the browser ask each team in turn would turn one page into one request per membership and
still leave the result unsorted.

The team scope is not a parameter. It is `principal.team_ids`, loaded from the caller's
own memberships on this request, so there is nothing to forge and no way to widen it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import PrincipalDep, SessionDep
from app.models.enums import Priority, Status, WorkItemType
from app.schemas.work_items import WorkItemPage, WorkItemSummary
from app.services import work_items
from app.services.cursors import decode_cursor
from app.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, cursor_for

router = APIRouter(prefix="/me", tags=["me"])


@router.get(
    "/items",
    response_model=WorkItemPage,
    summary="Work assigned to the caller, across all of their teams",
)
async def my_items(
    session: SessionDep,
    principal: PrincipalDep,
    item_status: Annotated[list[Status] | None, Query(alias="status")] = None,
    priority: Annotated[list[Priority] | None, Query()] = None,
    item_type: Annotated[list[WorkItemType] | None, Query(alias="type")] = None,
    q: Annotated[str | None, Query(max_length=200, description="Full-text search")] = None,
    cursor: Annotated[str | None, Query(description="`next_cursor` from the previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> WorkItemPage:
    """Newest first, with the same filters and keyset paging as a team's list.

    There is deliberately no `assignee` parameter: this *is* the assigned-to-me view, and
    accepting an assignee would turn it into a general cross-team item list — a much
    larger authorization surface for no operational need.
    """
    rows, has_more = await work_items.list_my_items(
        session,
        principal=principal,
        statuses=tuple(item_status or ()),
        priorities=tuple(priority or ()),
        types=tuple(item_type or ()),
        search=q,
        cursor=decode_cursor(cursor, id_kind="uuid") if cursor else None,
        limit=limit,
    )

    items: list[WorkItemSummary] = []
    for row in rows:
        role = principal.role_in(row.team_id)
        # The rows came from `team_id IN principal.team_ids`, so every row's team is one
        # the caller is in. Asserting states that invariant rather than silently
        # dropping a row and leaving the page short.
        assert role is not None
        items.append(WorkItemSummary.from_row(row, role=role))

    return WorkItemPage(
        items=items,
        next_cursor=cursor_for(rows, has_more=has_more),
        has_more=has_more,
        limit=limit,
    )
