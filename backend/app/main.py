"""FastAPI application factory.

Routers stay thin: they translate HTTP into calls on application services and
translate service results back into response schemas. Business rules live in
`app/services`, authorization in `app/policy`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.v1.router import api_router
from app.config import Settings, get_settings
from app.db import create_database
from app.errors import register_exception_handlers

API_V1_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Tests call this with their own settings."""
    resolved = settings or get_settings()
    logging.basicConfig(level=resolved.log_level.upper())

    # The engine is created eagerly so `app.state.db` is available to
    # dependencies without relying on startup events (which test transports and
    # some ASGI servers do not always run).
    database = create_database(resolved.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.db.dispose()

    app = FastAPI(
        title="Newtonite - Operations Under Pressure",
        description=(
            "Internal operational work management: team-scoped work items, a "
            "server-enforced workflow, an append-only activity history and "
            "deliberate handling of concurrent and retried requests."
        ),
        version="0.1.0",
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.settings = resolved
    app.state.db = database

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match"],
        expose_headers=["ETag", "Location", "Idempotent-Replay"],
    )

    app.include_router(health.router)
    app.include_router(api_router, prefix=API_V1_PREFIX)

    # Registered last so the envelope applies to every failure, including
    # framework-generated ones.
    register_exception_handlers(app)

    return app


app = create_app()
