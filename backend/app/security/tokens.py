"""Session token generation and hashing.

A session token is an opaque, unguessable bearer credential:

* **256 bits of `secrets` randomness**, base64url-encoded. There is nothing to
  guess and nothing to derive from it.
* **Stored only as a SHA-256 digest.** A database disclosure does not hand an
  attacker usable credentials, and it does not require a second factor to be
  safe.

A fast digest is correct here and deliberately different from the treatment of
user passwords (Argon2id, in `app.security.passwords`). The reason is entropy:
a 256-bit random value cannot be brute-forced, so slowing down verification
would only add latency to every authenticated request. A human-chosen password
can be guessed offline, so it gets a memory-hard KDF.

The same scheme is used for both access tokens (short-lived, header-borne) and
refresh tokens (long-lived, cookie-borne). The only difference is their TTL and
transport.
"""

from __future__ import annotations

import hashlib
import secrets

# Number of random bytes behind each token. 32 bytes = 256 bits.
TOKEN_BYTES = 32

# `secrets.token_urlsafe(32)` produces 43 characters.
TOKEN_LENGTH = 43

# Refresh tokens use the same entropy but a distinct cookie name and path.
REFRESH_TOKEN_BYTES = 32
REFRESH_TOKEN_COOKIE_NAME = "refresh_token"
REFRESH_TOKEN_COOKIE_PATH = "/api/v1/auth/refresh"


def generate_token() -> str:
    """Return a fresh, high-entropy session token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def generate_refresh_token() -> str:
    """Return a fresh, high-entropy refresh token."""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return the lower-case hex SHA-256 digest stored in the database.

    Lookup is by digest, so a database index on `token_hash` does the work and no
    timing-safe comparison is needed: the attacker never gets to compare against
    the stored value byte by byte.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = [
    "REFRESH_TOKEN_BYTES",
    "REFRESH_TOKEN_COOKIE_NAME",
    "REFRESH_TOKEN_COOKIE_PATH",
    "TOKEN_BYTES",
    "TOKEN_LENGTH",
    "generate_refresh_token",
    "generate_token",
    "hash_token",
]
