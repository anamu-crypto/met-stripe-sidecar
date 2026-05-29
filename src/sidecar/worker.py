"""Worker: drain ``webhook_events`` and dispatch to handlers.

Design notes
------------

**Concurrency.** Multiple worker processes can run safely against the same
database. The hot query uses ``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`` so
two workers never see the same row at the same time. Horizontal scaling is just
"run more containers".

**Crash safety.** We hold the row lock for the duration of processing inside a
single transaction. If the worker crashes or loses its DB connection mid-flight,
the transaction is rolled back, the row's ``attempts`` count is *not*
incremented, and another worker picks the row up. Trade-off: a Metronome API
call holds the row lock (and a DB connection) until it returns. With a strict
client-side timeout on the SDK (we set 30s) the worst case is a 30s lock —
acceptable for v0.1. If your Metronome volumes get large, switch to a
"claim row in tx 1, work outside tx, update in tx 2" pattern and add a sweeper
that resets stuck ``processing`` rows.

**Backoff.** Transient failures schedule
``next_attempt_at = now + min(base * 2^(attempts-1), cap) + jitter``. Permanent
failures (4xx other than 429, mapper errors, etc.) skip retries entirely.

**Loud failures.** When ``attempts >= max_attempts`` or we hit a permanent
error, the row is marked ``failed`` and an ``ERROR``-level structured log line
is emitted. Failed rows stay in the database for operator triage; you can
re-queue them manually by setting ``status='pending', attempts=0`` if/when the
root cause is fixed.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import signal
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sidecar.config import Settings, get_settings
from sidecar.db import dispose_engine, get_sessionmaker
from sidecar.handlers import (
    PermanentHandlerError,
    RetryableHandlerError,
    handle_customer_created,
    handle_customer_updated,
    handle_subscription_created,
    handle_subscription_deleted,
    handle_subscription_updated,
)
from sidecar.logging import configure_logging, get_logger
from sidecar.metronome_client import MetronomeClient
from sidecar.models import STATUS_FAILED, STATUS_PENDING, STATUS_PROCESSED, WebhookEvent

logger = get_logger(__name__)


# -----------------------------------------------------------------------------
# Handler registry. Adding a new event type later (e.g. customer.deleted or
# customer.subscription.updated) is a one-line change here.
#
# The return type is intentionally ``Any``: each handler module owns its own
# ``HandlerResult`` dataclass (the relevant fields differ between customer and
# subscription flows) and the worker doesn't use the value anyway. Tests and
# structured logs consume the per-handler ``HandlerResult`` directly.
# -----------------------------------------------------------------------------

HandlerFn = Callable[..., Awaitable[Any]]

HANDLERS: dict[str, HandlerFn] = {
    "customer.created": handle_customer_created,
    "customer.updated": handle_customer_updated,
    "customer.subscription.created": handle_subscription_created,
    # v0.2a: registered as explicit no-ops so the data-integrity gap surfaces
    # in logs (WARNING level, with `metronome_contract_*` fields) instead of
    # hiding inside the generic "unknown event type" path. v0.2b replaces
    # these with real propagation logic.
    "customer.subscription.updated": handle_subscription_updated,
    "customer.subscription.deleted": handle_subscription_deleted,
}


# -----------------------------------------------------------------------------
# Worker class — single-instance state (settings, metronome client, stop flag).
# -----------------------------------------------------------------------------


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        metronome: MetronomeClient,
    ) -> None:
        self._settings = settings
        self._metronome = metronome
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        """Ask the worker to drain the current event and exit."""
        self._stop_event.set()

    async def run(self) -> None:
        """Main loop: poll, process, sleep, repeat — until told to stop."""
        logger.info(
            "worker_started",
            extra={
                "event": "worker_started",
                "poll_interval_s": self._settings.worker_poll_interval_seconds,
                "max_attempts": self._settings.worker_max_attempts,
            },
        )

        while not self._stop_event.is_set():
            try:
                processed = await self._process_one()
            except Exception:
                logger.exception(
                    "worker_loop_unhandled_exception",
                    extra={"event": "worker_loop_unhandled_exception"},
                )
                processed = False

            if not processed:
                # No work waiting — sleep, but wake up immediately on shutdown.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._settings.worker_poll_interval_seconds,
                    )

        logger.info("worker_stopped", extra={"event": "worker_stopped"})

    # -------------------------------------------------------------------------
    # One event per iteration.
    # -------------------------------------------------------------------------

    async def _process_one(self) -> bool:
        """Try to process one event. Returns True iff a row was claimed.

        The entire claim + handle + status-update sequence runs inside one
        transaction so the row lock is held for the duration of work. See the
        module docstring for the trade-offs.
        """
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            event = await self._claim_next_event(session)
            if event is None:
                return False

            await self._handle_event(session, event)
            # session.begin() commits on context-exit if no exception.
            return True

    async def _claim_next_event(self, session: AsyncSession) -> WebhookEvent | None:
        """Pick the oldest ready-to-run pending event and lock the row.

        ``FOR UPDATE SKIP LOCKED`` is the standard "work queue in Postgres"
        primitive: another worker calling the same query at the same time will
        skip past the locked row and pick a different one.
        """
        now = datetime.now(UTC)
        stmt = (
            select(WebhookEvent)
            .where(
                WebhookEvent.status == STATUS_PENDING,
                WebhookEvent.next_attempt_at <= now,
            )
            .order_by(WebhookEvent.received_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _handle_event(self, session: AsyncSession, event: WebhookEvent) -> None:
        """Dispatch to the appropriate handler and update the row's status."""
        event.attempts += 1
        log_ctx: dict[str, Any] = {
            "stripe_event_id": event.stripe_event_id,
            "stripe_event_type": event.event_type,
            "attempt": event.attempts,
        }

        handler = HANDLERS.get(event.event_type)
        if handler is None:
            # Unknown event type. v0.1 only handles customer.{created,updated}.
            # Receivers shouldn't be filtering — instead we mark these processed
            # so they don't clog the queue, with a log line so they're visible.
            logger.info(
                "event_ignored_unknown_type",
                extra={"event": "event_ignored_unknown_type", **log_ctx},
            )
            event.status = STATUS_PROCESSED
            event.processed_at = datetime.now(UTC)
            event.last_error = None
            return

        try:
            await handler(
                session=session,
                metronome=self._metronome,
                settings=self._settings,
                stripe_event_id=event.stripe_event_id,
                event_payload=event.payload,
            )
        except PermanentHandlerError as exc:
            self._mark_failed(event, reason=str(exc), permanent=True)
            logger.error(
                "event_failed_permanent",
                extra={
                    "event": "event_failed_permanent",
                    "outcome": "failed",
                    "error": str(exc),
                    **log_ctx,
                },
            )
        except RetryableHandlerError as exc:
            self._schedule_retry_or_fail(event, reason=str(exc))
            logger.warning(
                "event_retry_scheduled" if event.status == STATUS_PENDING else "event_failed_exhausted",
                extra={
                    "event": (
                        "event_retry_scheduled"
                        if event.status == STATUS_PENDING
                        else "event_failed_exhausted"
                    ),
                    "outcome": "retry" if event.status == STATUS_PENDING else "failed",
                    "error": str(exc),
                    "next_attempt_at": event.next_attempt_at.isoformat(),
                    **log_ctx,
                },
            )
        except Exception as exc:
            # Unexpected. Treat as transient (safer default) but log fully.
            self._schedule_retry_or_fail(event, reason=f"{type(exc).__name__}: {exc}")
            logger.error(
                "event_unexpected_exception",
                extra={
                    "event": "event_unexpected_exception",
                    "outcome": "retry" if event.status == STATUS_PENDING else "failed",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    **log_ctx,
                },
            )
        else:
            event.status = STATUS_PROCESSED
            event.processed_at = datetime.now(UTC)
            event.last_error = None

    # -------------------------------------------------------------------------
    # Retry scheduling
    # -------------------------------------------------------------------------

    def _mark_failed(self, event: WebhookEvent, *, reason: str, permanent: bool) -> None:
        event.status = STATUS_FAILED
        event.last_error = (
            f"PERMANENT: {reason}" if permanent else f"EXHAUSTED RETRIES: {reason}"
        )
        event.processed_at = datetime.now(UTC)

    def _schedule_retry_or_fail(self, event: WebhookEvent, *, reason: str) -> None:
        if event.attempts >= self._settings.worker_max_attempts:
            self._mark_failed(event, reason=reason, permanent=False)
            return
        delay = _backoff_delay(
            attempts=event.attempts,
            base_seconds=self._settings.worker_retry_base_seconds,
            cap_seconds=self._settings.worker_retry_cap_seconds,
        )
        event.status = STATUS_PENDING
        event.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        event.last_error = reason


def _backoff_delay(
    *, attempts: int, base_seconds: float, cap_seconds: float
) -> float:
    """Compute exponential backoff with full jitter.

    Formula: ``random_uniform(0, min(base * 2^(attempts-1), cap))``.

    Full jitter prevents the "thundering herd" pattern where many failed
    events from the same outage all retry on the same schedule. See
    https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/.
    """
    if attempts < 1:
        attempts = 1
    raw = base_seconds * (2 ** (attempts - 1))
    capped = min(raw, cap_seconds)
    # Full jitter: a uniform draw between 0 and the capped delay. We add a
    # small floor (1s) so we never retry instantly even with bad config.
    return max(1.0, random.uniform(0.0, capped))


# -----------------------------------------------------------------------------
# Entrypoint for `python -m sidecar.worker` / docker-compose worker service.
# -----------------------------------------------------------------------------


async def _amain() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    metronome = MetronomeClient(
        api_key=settings.metronome_api_key.get_secret_value(),
        base_url=settings.metronome_base_url,
    )
    worker = Worker(settings=settings, metronome=metronome)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.request_stop)

    try:
        await worker.run()
    finally:
        await metronome.aclose()
        await dispose_engine()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
