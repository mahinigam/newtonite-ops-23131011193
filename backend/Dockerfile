# syntax=docker/dockerfile:1
#
# The API image.
#
# `uv` is used here exactly as it is locally: the lockfile is the source of truth, so the
# image installs the same dependency versions the tests ran against. Dependencies are copied
# and installed before the source so that editing a Python file does not reinstall the world.
#
# Migrations run from the entrypoint rather than from application startup. A web process
# that creates tables as a side effect of booting is the thing this project deliberately
# avoids; the entrypoint makes the step explicit and lets the healthcheck wait for it.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# A non-root user for the application process. The files it needs are world-readable.
RUN useradd --create-home --uid 10001 newtonite \
    && chown -R newtonite:newtonite /app
USER newtonite

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
