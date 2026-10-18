"""Liveness and readiness endpoints."""

from __future__ import annotations

from typing import Any

from app.api.health import readyz
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError


class _UnavailableSession:
    """A session whose only database call fails, simulating an outage."""

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("database offline"))


async def test_healthz_reports_process_up(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_proves_a_database_round_trip(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


async def test_readyz_reports_503_when_the_database_is_unavailable() -> None:
    response = await readyz(_UnavailableSession())  # type: ignore[arg-type]

    assert response.status_code == 503
    assert response.body == b'{"status":"not_ready","database":"unavailable"}'
