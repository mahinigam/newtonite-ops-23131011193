"""The outbox worker: delivers queued notifications.

Run as its own process:

    uv run python -m app.worker            # poll forever
    uv run python -m app.worker --once     # drain what is due, then exit

Why a separate process rather than a background task in the API
---------------------------------------------------------------
The API's job is to answer requests, and a notification that is slow to deliver must never
slow down an assignment. More importantly, a background task inside the web process dies
with it: a deploy would silently stop delivery until the next request arrived. A separate
worker is the smallest thing that survives a restart without adding Redis, Celery, or
another broker — the queue is already a PostgreSQL table.

How a batch is processed
------------------------
1. `claim_batch` locks up to `batch_size` due rows with `FOR UPDATE SKIP LOCKED`. Two
   workers can run side by side; neither waits for the other.
2. Each event is handled inside its **own savepoint**. A handler that raises rolls back
   only that event's work, so one bad payload cannot take the rest of the batch with it —
   and the outer transaction stays usable for recording the failure.
3. Success marks the row `delivered`; failure records the attempt and either schedules a
   retry with backoff or, at the limit, marks it `failed`.
4. Everything commits together. A crash before the commit leaves the rows pending, so the
   next poll retries them. That is the at-least-once half of the guarantee; the notification
   table's unique constraint is the exactly-once half.

A permanently failed event is not silent: it is counted by the operator summary and
logged, with its last error retained on the row.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import Database, create_database
from app.services import notifications, outbox

logger = logging.getLogger(__name__)

#: A handler consumes one claimed event. Returns whether it created a notification;
#: the return value is for tests and logs, not for the delivery decision.
Handler = Callable[[AsyncSession, Any], Coroutine[Any, Any, bool]]


async def process_batch(
    session: AsyncSession,
    *,
    handler: Handler = notifications.deliver,
    limit: int = 20,
    max_attempts: int = outbox.DEFAULT_MAX_ATTEMPTS,
) -> int:
    """Deliver up to `limit` due events and commit. Returns how many were processed.

    The handler is a parameter so the retry and exhaustion paths can be tested with a
    handler that fails on purpose. Everything else is the production path.
    """
    events = await outbox.claim_batch(session, limit=limit, max_attempts=max_attempts)
    if not events:
        return 0

    for event in events:
        try:
            # A savepoint per event: a failure here must not abort the batch's
            # transaction, because the failure itself has to be recorded in it.
            async with session.begin_nested():
                await handler(session, event)
        except Exception as exc:
            attempts = event.attempts + 1
            exhausted = await outbox.mark_failed(
                session,
                event_id=event.id,
                error=f"{type(exc).__name__}: {exc}",
                attempts=attempts,
                max_attempts=max_attempts,
            )
            if exhausted:
                logger.error(
                    "outbox event %s (%s) failed permanently after %s attempt(s): %s",
                    event.id,
                    event.event_type,
                    attempts,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                logger.warning(
                    "outbox event %s (%s) attempt %s/%s failed, retrying: %s",
                    event.id,
                    event.event_type,
                    attempts,
                    max_attempts,
                    f"{type(exc).__name__}: {exc}",
                )
        else:
            await outbox.mark_delivered(session, event_id=event.id)

    await session.commit()
    return len(events)


async def drain(
    database: Database,
    *,
    handler: Handler = notifications.deliver,
    batch_size: int = 20,
    max_attempts: int = outbox.DEFAULT_MAX_ATTEMPTS,
    max_batches: int = 200,
) -> int:
    """Process until nothing is due, or until `max_batches` is reached.

    Used by `--once` and by the end-to-end check, where waiting for a poll interval would
    only make the test slower. The bound keeps a permanently-due row from making this
    loop forever.
    """
    total = 0
    for _ in range(max_batches):
        async with database.session_factory() as session:
            processed = await process_batch(
                session, handler=handler, limit=batch_size, max_attempts=max_attempts
            )
        if processed == 0:
            break
        total += processed
    return total


async def run(
    database: Database,
    *,
    stop: asyncio.Event,
    handler: Handler = notifications.deliver,
    batch_size: int = 20,
    poll_interval: float = 1.0,
    max_attempts: int = outbox.DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Poll until `stop` is set.

    An idle queue sleeps for `poll_interval` rather than spinning, and a busy one drains
    back-to-back batches without sleeping — so latency stays low under load and the
    database is left alone when there is nothing to do.
    """
    logger.info("outbox worker started (batch=%s, poll=%.1fs)", batch_size, poll_interval)
    while not stop.is_set():
        async with database.session_factory() as session:
            try:
                processed = await process_batch(
                    session, handler=handler, limit=batch_size, max_attempts=max_attempts
                )
            except Exception:  # pragma: no cover - connection-level failure
                logger.exception("outbox batch failed; retrying after the poll interval")
                processed = 0

        if processed > 0:
            logger.debug("delivered a batch of %s outbox event(s)", processed)
            continue

        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except TimeoutError:
            pass
    logger.info("outbox worker stopped")


def _settings_for_worker(settings: Settings) -> dict[str, Any]:
    return {
        "batch_size": settings.outbox_batch_size,
        "poll_interval": settings.outbox_poll_interval_seconds,
        "max_attempts": settings.outbox_max_attempts,
    }


async def _serve(*, once: bool) -> None:
    settings = get_settings()
    database = create_database(settings.database_url)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - not POSIX
            pass

    options = _settings_for_worker(settings)
    try:
        if once:
            delivered = await drain(database, **options)
            logger.info("drained %s outbox event(s)", delivered)
        else:
            await run(database, stop=stop, **options)
    finally:
        await database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deliver queued outbox events as in-app notifications."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="drain the events that are due, then exit (useful for demos and checks)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=get_settings().log_level.upper())
    asyncio.run(_serve(once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
