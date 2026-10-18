"""Shared test helpers.

Kept out of `conftest.py` so ordinary modules can import them without relying on
pytest's special handling of the conftest module.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Child tables first. CASCADE makes the order non-critical, but being explicit
# documents the relationships. RESTART IDENTITY keeps sequences deterministic.
ALL_TABLES = (
    "notifications",
    "outbox_events",
    "activity_events",
    "comments",
    "work_items",
    "memberships",
    "idempotency_keys",
    "auth_sessions",
    "users",
    "teams",
)

# Alembic's own bookkeeping table, excluded from domain-schema assertions.
ALEMBIC_TABLE = "alembic_version"

TRUNCATE_STATEMENT = text(f"TRUNCATE TABLE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")

# Deterministic identifiers so failures are easy to read and fixtures can be
# shared across suites.
TEAM_ALPHA = "11111111-1111-1111-1111-111111111111"
TEAM_BETA = "22222222-2222-2222-2222-222222222222"
USER_ALPHA_MEMBER = "aaaaaaaa-0000-0000-0000-000000000001"
USER_BETA_MEMBER = "aaaaaaaa-0000-0000-0000-000000000002"


def alembic_config(database_url: str) -> Config:
    """Alembic configuration pointed at `database_url`.

    Passing the URL explicitly is how the suite targets the throwaway test
    database instead of the development database from `.env`.
    """
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(BACKEND_ROOT))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


# Two teams whose members do not overlap. Used to prove team boundaries.
#
# asyncpg prepares each statement individually and refuses to send several
# commands in one prepared statement, so the seed is executed statement by
# statement rather than as one multi-statement script.
_SEED_STATEMENTS: tuple[TextClause, ...] = (
    text(
        """
        INSERT INTO teams (id, key, name) VALUES
          (:team_alpha, 'alpha', 'Alpha'),
          (:team_beta, 'beta', 'Beta')
        """
    ),
    text(
        """
        INSERT INTO users (id, email, display_name, password_hash) VALUES
          (:user_alpha, 'alpha.member@newtonite.dev', 'Alpha Member', 'placeholder-hash'),
          (:user_beta, 'beta.member@newtonite.dev', 'Beta Member', 'placeholder-hash')
        """
    ),
    text(
        """
        INSERT INTO memberships (user_id, team_id, role) VALUES
          (:user_alpha, :team_alpha, 'member'),
          (:user_beta, :team_beta, 'member')
        """
    ),
)

SEED_TWO_TEAMS_PARAMS = {
    "team_alpha": TEAM_ALPHA,
    "team_beta": TEAM_BETA,
    "user_alpha": USER_ALPHA_MEMBER,
    "user_beta": USER_BETA_MEMBER,
}


async def seed_two_teams(db: AsyncSession) -> None:
    """Create two isolated teams, each with one member, and commit."""
    for statement in _SEED_STATEMENTS:
        await db.execute(statement, SEED_TWO_TEAMS_PARAMS)
    await db.commit()
