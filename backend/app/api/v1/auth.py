"""Authentication endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from sqlalchemy import select

from app.api.deps import PrincipalDep, SessionDep, SettingsDep
from app.config import Settings
from app.models import AuthSession, User
from app.schemas.auth import (
    LoginRequest,
    LogoutResponse,
    MembershipSummary,
    MeResponse,
    TokenPairResponse,
    UserSummary,
)
from app.security.tokens import REFRESH_TOKEN_COOKIE_NAME, REFRESH_TOKEN_COOKIE_PATH
from app.services import auth as auth_service
from app.services import memberships as membership_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_refresh_cookie(
    response: Response, refresh_token: str, expires_at: datetime, settings: Settings
) -> None:
    """Set the HttpOnly refresh token cookie."""
    max_age = int((expires_at - datetime.now(expires_at.tzinfo)).total_seconds())
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        value=refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.environment == "production",
        samesite="strict",
        path=REFRESH_TOKEN_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Clear the refresh token cookie."""
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        value="",
        max_age=0,
        httponly=True,
        secure=True,
        samesite="strict",
        path=REFRESH_TOKEN_COOKIE_PATH,
    )


@router.post(
    "/login",
    response_model=TokenPairResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange credentials for an access/refresh token pair",
)
async def login(
    response: Response,
    payload: LoginRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> TokenPairResponse:
    """Start a session by issuing an access/refresh token pair.

    The access token is returned in the response body and must be sent as
    `Authorization: Bearer <token>` on subsequent requests.

    The refresh token is set as an `HttpOnly; Secure; SameSite=Strict` cookie
    scoped to `/api/v1/auth/refresh`. It is not returned in the response body.
    """
    user, issued = await auth_service.login(
        session, email=payload.email, password=payload.password, settings=settings
    )
    _set_refresh_cookie(response, issued.refresh_token, issued.refresh_expires_at, settings)
    return TokenPairResponse(
        access_token=issued.access_token,
        access_expires_at=issued.access_expires_at,
        refresh_expires_at=issued.refresh_expires_at,
        user=UserSummary.model_validate(user),
    )


@router.post(
    "/refresh",
    response_model=TokenPairResponse,
    status_code=status.HTTP_200_OK,
    summary="Rotate the refresh token and issue a new access token",
)
async def refresh(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_TOKEN_COOKIE_NAME),
) -> TokenPairResponse:
    """Rotate the refresh token and issue a new access/refresh pair.

    The refresh token must be present in the `refresh_token` cookie (set by
    `/login` or a previous `/refresh` call). It is not accepted in the body.

    On success, a new access token is returned in the body and a new refresh
    token is set in the cookie. The old refresh token is revoked.

    If the refresh token is missing, expired, revoked, or has been reused
    (rotation chain broken), a `401 invalid_session` is returned and the cookie
    is cleared.
    """
    if refresh_token is None:
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "invalid_session", "message": "Refresh token missing"}},
        )

    try:
        issued = await auth_service.rotate_refresh_token(
            session, refresh_token=refresh_token, settings=settings
        )
    except Exception as e:
        _clear_refresh_cookie(response)
        from app.errors import InvalidSession

        if isinstance(e, InvalidSession):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "code": "invalid_session",
                        "message": "Session expired or invalid. Please sign in again.",
                    }
                },
            ) from e
        raise

    _set_refresh_cookie(response, issued.refresh_token, issued.refresh_expires_at, settings)
    access_session = (
        await session.execute(select(AuthSession).where(AuthSession.id == issued.access_session_id))
    ).scalar_one()
    user = (
        await session.execute(select(User).where(User.id == access_session.user_id))
    ).scalar_one()

    return TokenPairResponse(
        access_token=issued.access_token,
        access_expires_at=issued.access_expires_at,
        refresh_expires_at=issued.refresh_expires_at,
        user=UserSummary.model_validate(user),
    )


@router.post(
    "/logout", response_model=LogoutResponse, summary="Revoke the current session and refresh token"
)
async def logout(
    response: Response,
    session: SessionDep,
    principal: PrincipalDep,
) -> LogoutResponse:
    """Sign out by revoking both the access session and its refresh token.

    The refresh token cookie is cleared.
    """
    revoked = await auth_service.revoke(session, session_id=principal.session_id)
    # Also revoke all refresh tokens for this user to ensure complete logout
    await auth_service.revoke_all_refresh_tokens_for_user(session, user_id=principal.user.id)
    _clear_refresh_cookie(response)
    return LogoutResponse(revoked=revoked)


@router.get("/me", response_model=MeResponse, summary="The caller and their teams")
async def me(session: SessionDep, principal: PrincipalDep) -> MeResponse:
    """Describe the authenticated caller.

    Returns their role in each team they belong to, which is what the frontend uses
    to decide what to offer. The roles are advisory to the UI only: every write is
    authorized again on the server.
    """
    teams = await membership_service.list_teams_for_user(session, user_id=principal.user.id)
    return MeResponse(
        user=UserSummary.model_validate(principal.user),
        memberships=[MembershipSummary.model_validate(team) for team in teams],
    )
