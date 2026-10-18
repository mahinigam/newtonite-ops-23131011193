"""Operator-facing visibility.

The outbox makes notifications asynchronous, which introduces a failure mode that a
synchronous implementation would not have: an event can be queued and never delivered.
Being asynchronous is only acceptable if that state is *visible*, so this endpoint exposes
delivery counts per team.

It is scoped to teams where the caller is an **admin**, and it returns counts only. There
is no global operator account in this domain — roles are per team — so "admin of at least
one team" is the narrowest correct gate. Counts carry no item titles, no recipients and no
payloads, so even the gate is not load-bearing for confidentiality.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import PrincipalDep, SessionDep
from app.errors import Forbidden
from app.models.enums import OutboxStatus, Role
from app.schemas.ops import OutboxSummary, OutboxTeamSummary
from app.services import outbox

router = APIRouter(prefix="/ops", tags=["operations"])


@router.get(
    "/outbox",
    response_model=OutboxSummary,
    summary="Outbox delivery counts for teams the caller administers",
)
async def outbox_summary(session: SessionDep, principal: PrincipalDep) -> OutboxSummary:
    """Pending, delivered and permanently failed counts.

    `failed` is the number that matters: it is the only terminal state, so a non-zero
    value means an event exhausted its retries and nothing else will happen to it. Every
    other row will be retried.
    """
    admin_team_ids = tuple(
        team_id for team_id, role in principal.roles.items() if role is Role.ADMIN
    )
    if not admin_team_ids:
        raise Forbidden("Only a team admin can inspect outbox delivery.")

    rows = await outbox.summarise(session, team_ids=admin_team_ids)

    # Pivot (team, status, count) rows into one summary per team, defaulting absent
    # statuses to zero so a team that has never queued anything still reads correctly.
    counts_by_team: dict[uuid.UUID, dict[str, int]] = {}
    key_by_team: dict[uuid.UUID, str] = {}
    for row in rows:
        key_by_team[row.team_id] = row.team_key
        counts_by_team.setdefault(row.team_id, {})[row.status] = int(row.count)

    teams = [
        OutboxTeamSummary(
            team_id=team_id,
            team_key=key_by_team[team_id],
            pending=counts.get(OutboxStatus.PENDING.value, 0),
            delivered=counts.get(OutboxStatus.DELIVERED.value, 0),
            failed=counts.get(OutboxStatus.FAILED.value, 0),
        )
        for team_id, counts in counts_by_team.items()
    ]
    teams.sort(key=lambda team: team.team_key)

    return OutboxSummary(
        teams=teams,
        pending=sum(team.pending for team in teams),
        delivered=sum(team.delivered for team in teams),
        failed=sum(team.failed for team in teams),
    )
