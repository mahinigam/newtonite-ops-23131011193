"""Authentication request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.models.enums import Role
from app.schemas import RequestModel, ResponseModel


class LoginRequest(RequestModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        """Lower-case and trim.

        Addresses are stored normalised, so doing it at the boundary means
        `Ada.Admin@Example.COM ` and `ada.admin@example.com` are the same account
        rather than two.
        """
        return value.strip().lower()


class UserSummary(ResponseModel):
    id: uuid.UUID
    email: str
    display_name: str


class MembershipSummary(ResponseModel):
    team_id: uuid.UUID
    team_key: str
    team_name: str
    role: Role


class TokenPairResponse(ResponseModel):
    """Token pair returned by login and refresh.

    The access token is carried in the `Authorization: Bearer` header.
    The refresh token is delivered via an HttpOnly cookie scoped to
    `/api/v1/auth/refresh` and does not appear in this response body.
    """

    access_token: str
    access_token_type: str = "Bearer"
    access_expires_at: datetime
    refresh_expires_at: datetime
    user: UserSummary


class MeResponse(ResponseModel):
    user: UserSummary
    memberships: list[MembershipSummary]


class LogoutResponse(ResponseModel):
    revoked: bool
