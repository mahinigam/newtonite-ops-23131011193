"""Unversioned operational endpoints used by operators and CI."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Reports that the process is up. Deliberately does not touch the database."""
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    """Reports readiness by proving a database round-trip works.

    A database outage is an expected operational state, not a server bug: return
    ``503 Service Unavailable`` so load balancers and Compose health checks stop
    sending traffic here. The body deliberately contains no driver detail.
    """
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Readiness check failed: database unavailable")
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable"},
        )
    return JSONResponse(content={"status": "ready", "database": "ok"})
