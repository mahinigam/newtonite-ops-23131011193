"""Comment endpoints.

Comments are addressed through the item they belong to — `/items/{item_id}/comments` —
rather than as a top-level resource, because a comment has no meaning outside its item.
The team is therefore derived from the **item's own row** and the caller is authorized
against that, never against anything the client sent.

Creating requires an `Idempotency-Key`, for the same reason creating an item does: a
double-click or a retry must not post the same note twice. Reading requires nothing beyond
team membership, so a viewer can read the discussion.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.deps import PrincipalDep, SessionDep
from app.schemas.comments import CommentPage, CommentResponse, CreateCommentRequest
from app.services import comments, idempotency
from app.services.cursors import decode_cursor
from app.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, cursor_for

router = APIRouter(tags=["comments"])


@router.post(
    "/items/{item_id}/comments",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Comment on a work item (requires an Idempotency-Key)",
)
async def create_comment(
    item_id: uuid.UUID,
    payload: CreateCommentRequest,
    session: SessionDep,
    principal: PrincipalDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CommentResponse:
    """Add a comment, exactly once even if the request is delivered twice.

    `Idempotency-Key` is required: the key row, the comment and the `commented` history
    event are written in one transaction, so a retry either replays the comment that
    exists or creates one — never two, and never a comment whose history is missing.

    `201 Created` for the request that created the comment, `200 OK` with
    `Idempotent-Replay: true` for a replay of the same request. The same key with a
    different body is `409 idempotency_key_reuse`.
    """
    key = idempotency.validate_key(idempotency_key)

    outcome = await comments.create_comment(
        session,
        principal=principal,
        item_id=item_id,
        request=payload,
        idempotency_key=key,
    )

    if outcome.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"
    return CommentResponse.from_row(outcome.comment)


@router.get(
    "/items/{item_id}/comments",
    response_model=CommentPage,
    summary="Read an item's comments, newest first",
)
async def list_comments(
    item_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
    cursor: Annotated[str | None, Query(description="`next_cursor` from the previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CommentPage:
    """Newest first, with its own cursor.

    The cursor names a position in this thread. It uses the same UUID-keyed syntax as an
    item list, so a mixed-up cursor is accepted but names a different position; clients
    must keep each cursor with the list that issued it. An item in a team the caller does
    not belong to is `404`, exactly as an id that was never issued.
    """
    rows, has_more = await comments.list_comments(
        session,
        principal=principal,
        item_id=item_id,
        cursor=decode_cursor(cursor, id_kind="uuid") if cursor else None,
        limit=limit,
    )

    return CommentPage(
        items=[CommentResponse.from_row(row) for row in rows],
        next_cursor=cursor_for(rows, has_more=has_more),
        has_more=has_more,
        limit=limit,
    )
