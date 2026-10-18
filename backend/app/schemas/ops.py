"""Operational visibility schemas.

Deliberately counts, not payloads. An operator needs to know whether the queue is
draining; they do not need to read other people's notifications to find out.
"""

from __future__ import annotations

import uuid

from app.schemas import ResponseModel


class OutboxTeamSummary(ResponseModel):
    team_id: uuid.UUID
    team_key: str
    pending: int
    delivered: int
    failed: int


class OutboxSummary(ResponseModel):
    teams: list[OutboxTeamSummary]
    pending: int
    delivered: int
    failed: int


__all__ = ["OutboxSummary", "OutboxTeamSummary"]
