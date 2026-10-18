"""Print the query plans behind the numbers in `docs/DATA_MODEL.md`.

This is a measuring instrument, not part of the application or the test suite. It exists
so the performance claims in the documentation can be reproduced rather than taken on
trust:

    uv run python -m scripts.explain_lists

It imports the service layer's statement builders — `list_statement`,
`comment_list_statement`, `item_activity_statement`, `team_activity_statement` — so the
SQL it explains is the SQL the API actually executes. A hand-written approximation would
be easier to write and would drift the first time a filter was added.

Run it against the seeded development database. On an empty database every plan is a
sequential scan, because the tables are too small for an index to win — the numbers only
mean something once there are a few thousand rows to walk past.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from app.config import get_settings
from app.db import create_database
from app.models.enums import Priority, Status
from app.services.activity import item_activity_statement, team_activity_statement
from app.services.comments import comment_list_statement
from app.services.cursors import Cursor
from app.services.work_items import list_statement, my_items_statement
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.ext.asyncio import AsyncSession

# Named with an explicit signature because SQLAlchemy's stubs leave `PGDialect.__init__`
# untyped, and mypy refuses an untyped call from annotated code. Calling it is the
# documented way to obtain a dialect instance for compilation.
POSTGRES_DIALECT: Callable[[], Dialect] = postgresql.dialect

# Taken from the seed rather than hard-coded as UUIDs, so this keeps working if the seed
# is regenerated and identifiers change.
TEAM_KEY = "platform"
VIEWER_EMAIL = "sam.multi@newtonite.dev"

#: How far into the team feed the deep-page measurement starts, as a fraction of the
#: feed: half way, which is deep enough to prove the index is walked rather than scanned,
#: and works at any seed size. (The seed's per-item timelines are only a handful of events
#: long, so the team feed is the list where a genuine "somewhere in the middle" position
#: exists.)
DEEP_OFFSET_FRACTION = 2


def compile_sql(statement: Any) -> str:
    """Render a statement as SQL with values inlined, ready for `EXPLAIN`.

    Inlining is deliberate: a parameterised `EXPLAIN` cannot be asked about a specific
    value, and the point here is the plan for a realistic one. The statements measured
    come from the service layer, never from user input.
    """
    return str(
        statement.compile(dialect=POSTGRES_DIALECT(), compile_kwargs={"literal_binds": True})
    )


async def resolve_seed_ids(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    team_id = (
        await session.execute(text("SELECT id FROM teams WHERE key = :key"), {"key": TEAM_KEY})
    ).scalar_one()
    viewer_id = (
        await session.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": VIEWER_EMAIL}
        )
    ).scalar_one()
    return team_id, viewer_id


async def viewer_team_ids(session: AsyncSession, viewer_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    """Every team the measured viewer belongs to.

    `/me/items` scopes itself to the caller's memberships, so measuring it with one team
    would measure a shape the endpoint never issues for a multi-team account.
    """
    rows = await session.execute(
        text("SELECT team_id FROM memberships WHERE user_id = :viewer"),
        {"viewer": viewer_id},
    )
    return tuple(row[0] for row in rows)


async def deep_item_cursor(session: AsyncSession, team_id: uuid.UUID) -> Cursor:
    """A cursor from 300 rows into the team's items.

    A hand-picked timestamp would make the "deep page" measurement meaningless — it would
    either match nothing or everything. Reading a real position means the plan has
    somewhere genuine to start walking from.
    """
    row = (
        await session.execute(
            text(
                "SELECT created_at, id FROM work_items WHERE team_id = :team "
                "ORDER BY created_at DESC, id DESC OFFSET 300 LIMIT 1"
            ),
            {"team": team_id},
        )
    ).one()
    return Cursor(created_at=row[0], id=row[1])


async def deep_event_cursor(session: AsyncSession, team_id: uuid.UUID) -> Cursor:
    """A cursor deep inside one team's activity feed.

    The id is a bigint here, not a UUID — `activity_events` is keyed by an identity — so
    the cursor carries an integer, which is what the codec's `id_kind` exists for.
    """
    offset = (
        await session.execute(
            text("SELECT count(*) / :fraction FROM activity_events WHERE team_id = :team"),
            {"team": team_id, "fraction": DEEP_OFFSET_FRACTION},
        )
    ).scalar_one()
    row = (
        await session.execute(
            text(
                "SELECT created_at, id FROM activity_events WHERE team_id = :team "
                "ORDER BY created_at DESC, id DESC OFFSET :offset LIMIT 1"
            ),
            {"team": team_id, "offset": offset},
        )
    ).one()
    return Cursor(created_at=row[0], id=int(row[1]))


async def busiest_item(session: AsyncSession, *, table: str) -> uuid.UUID:
    """The item with the most rows in `table`, so the measurement uses real data.

    The seed's per-item timelines and threads are short, so this picks the longest of
    them; the team feed is measured separately because it is the list with thousands of
    rows behind it. The two queries are spelled out rather than built from `table`,
    because interpolating a name into SQL is a habit worth not having even here.
    """
    queries = {
        "activity_events": text(
            "SELECT work_item_id FROM activity_events GROUP BY work_item_id "
            "ORDER BY count(*) DESC, work_item_id LIMIT 1"
        ),
        "comments": text(
            "SELECT work_item_id FROM comments GROUP BY work_item_id "
            "ORDER BY count(*) DESC, work_item_id LIMIT 1"
        ),
    }
    item_id: uuid.UUID = (await session.execute(queries[table])).scalar_one()
    return item_id


async def explain(session: AsyncSession, label: str, sql: str) -> None:
    lines = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"))).scalars().all()
    plan = "\n".join(str(line) for line in lines)
    timing = next(
        (line.strip() for line in plan.splitlines() if "Execution Time" in line),
        "no timing reported",
    )
    print("=" * 78)
    print(label)
    print("=" * 78)
    print(plan)
    print(f"--> {timing}")
    print()


async def main() -> None:
    database = create_database(get_settings().database_url)
    try:
        async with database.session_factory() as session:
            team_id, viewer_id = await resolve_seed_ids(session)
            event_item = await busiest_item(session, table="activity_events")
            comment_item = await busiest_item(session, table="comments")
            viewer_teams = await viewer_team_ids(session, viewer_id)

            # Built eagerly, one statement per measured shape, so the SQL under each
            # heading is visible in this file rather than assembled from a filter dict.
            cases: list[tuple[str, Any]] = [
                (
                    "team list, first page (no filter)",
                    list_statement(team_id=team_id, viewer_id=viewer_id, limit=25),
                ),
                (
                    "team list, filtered by status",
                    list_statement(
                        team_id=team_id,
                        viewer_id=viewer_id,
                        limit=25,
                        statuses=(Status.OPEN,),
                    ),
                ),
                (
                    "team list, filtered by status and priority",
                    list_statement(
                        team_id=team_id,
                        viewer_id=viewer_id,
                        limit=25,
                        statuses=(Status.IN_PROGRESS,),
                        priorities=(Priority.HIGH, Priority.URGENT),
                    ),
                ),
                (
                    "queue: unassigned high-or-urgent items",
                    list_statement(
                        team_id=team_id,
                        viewer_id=viewer_id,
                        limit=25,
                        unassigned=True,
                        priorities=(Priority.HIGH, Priority.URGENT),
                    ),
                ),
                (
                    "queue: unassigned high-or-urgent OPEN items (the partial index's shape)",
                    list_statement(
                        team_id=team_id,
                        viewer_id=viewer_id,
                        limit=25,
                        unassigned=True,
                        statuses=(Status.OPEN,),
                        priorities=(Priority.HIGH, Priority.URGENT),
                    ),
                ),
                (
                    "dashboard: assigned to me",
                    list_statement(team_id=team_id, viewer_id=viewer_id, limit=25, only_mine=True),
                ),
                (
                    # The `/me/items` shape: the assignee index, restricted to the caller's
                    # own teams. Unlike the team-scoped dashboard row above, there is no
                    # `team_id` predicate to intersect with, so this is a single index scan.
                    "cross-team: my work (/me/items)",
                    my_items_statement(viewer_id=viewer_id, team_ids=viewer_teams, limit=25),
                ),
                (
                    "search: full text",
                    list_statement(
                        team_id=team_id,
                        viewer_id=viewer_id,
                        limit=25,
                        search="payment failed",
                    ),
                ),
                (
                    "team list, deep page by cursor",
                    list_statement(
                        team_id=team_id,
                        viewer_id=viewer_id,
                        limit=25,
                        cursor=await deep_item_cursor(session, team_id),
                    ),
                ),
                (
                    "item comments, newest first (the busiest thread in the seed)",
                    comment_list_statement(work_item_id=comment_item, limit=25),
                ),
                (
                    "item timeline, newest first (the busiest timeline in the seed)",
                    item_activity_statement(work_item_id=event_item, limit=25),
                ),
                (
                    "team activity feed, first page",
                    team_activity_statement(team_id=team_id, limit=25),
                ),
                (
                    "team activity feed, deep page by cursor",
                    team_activity_statement(
                        team_id=team_id,
                        limit=25,
                        cursor=await deep_event_cursor(session, team_id),
                    ),
                ),
            ]

            for label, statement in cases:
                await explain(session, label, compile_sql(statement))
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
