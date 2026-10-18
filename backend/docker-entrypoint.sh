#!/bin/sh
#
# Bring the database to head, optionally load the demo data, then run the container's
# command. The seed script is idempotent, so a restart does not duplicate anything.

set -eu

alembic upgrade head

if [ "${SEED_ON_START:-0}" = "1" ]; then
    python -m scripts.seed --items "${SEED_ITEMS:-3000}"
fi

exec "$@"
