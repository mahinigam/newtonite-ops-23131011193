"""Authentication sessions and refresh tokens.

The API issues two kinds of opaque random bearer tokens at login:

1. **Access token** (short-lived, 5 min default) — carried in the `Authorization:
   Bearer` header. Only its SHA-256 digest is stored. Validated on every request.

2. **Refresh token** (long-lived, 7 days default) — delivered via an `HttpOnly;
   Secure; SameSite=Strict` cookie scoped to `/api/v1/auth/refresh`. Only its
   SHA-256 digest is stored. Used solely to obtain new access tokens.

Both token types are rejected when any of the following holds:

* the digest is unknown,
* `expires_at` has passed (absolute lifetime),
* `revoked_at` is set (logout or rotation),
* for access tokens only: `last_used_at` is older than the idle window.

A fast digest (SHA-256) is correct for both: 256 bits of `secrets` randomness
cannot be brute-forced, unlike a low-entropy user password which gets Argon2id.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# SHA-256 rendered as lower-case hex.
TOKEN_HASH_LENGTH = 64


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(lazy="raise")

    __table_args__ = (
        # Supports "revoke every session for this user" and per-user auditing.
        Index("ix_auth_sessions_user_id", "user_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuthSession {self.id} user={self.user_id}>"


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When a refresh token is rotated, the new token records the hash of the one
    # it replaced. This enables reuse detection: if a revoked token's hash appears
    # again, the rotation chain is broken and the entire family is revoked.
    replaced_by_token_hash: Mapped[str | None] = mapped_column(
        String(TOKEN_HASH_LENGTH), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(lazy="raise")

    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_replaced_by", "replaced_by_token_hash"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RefreshToken {self.id} user={self.user_id}>"


from app.models.user import User  # noqa: E402  (resolves the relationship target)

__all__ = ["TOKEN_HASH_LENGTH", "AuthSession", "RefreshToken", "User"]
