"""Aggregates every v1 router under a single mount point."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import activity, auth, comments, items, me, notifications, ops, teams, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(comments.router)
api_router.include_router(activity.router)
api_router.include_router(items.router)
api_router.include_router(me.router)
api_router.include_router(notifications.router)
api_router.include_router(ops.router)
api_router.include_router(teams.router)
api_router.include_router(users.router)
