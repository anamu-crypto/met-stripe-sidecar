"""FastAPI receiver: ``POST /webhooks/stripe`` and ``GET /health``.

Responsibilities of the receiver — and *only* these:

  1. Verify the Stripe webhook signature against the raw request body.
  2. Insert the event into ``webhook_events`` with
     ``ON CONFLICT (stripe_event_id) DO NOTHING`` for idempotency.
  3. Return 200 within milliseconds.

The receiver does **not** call Metronome and does **not** block on slow work.
Stripe retries webhooks that don't respond within ~10s and gives up after a
few days, so a slow receiver causes both cascading retries and missed events.
The worker is what eventually calls Metronome.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sidecar.config import get_settings
from sidecar.db import dispose_engine, session_scope
from sidecar.logging import configure_logging, get_logger
from sidecar.models import WebhookEvent
from sidecar.stripe_client import InvalidWebhookSignature, verify_and_parse_event

logger = get_logger(__name__)


# -----------------------------------------------------------------------------
# Lifespan: do exactly the minimum we need at process startup/shutdown.
# Migrations are run by a separate process (`alembic upgrade head`) — see the
# `migrate` service in docker-compose.yml — *not* here.
# -----------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "server_started",
        extra={"event": "server_started", "port": settings.port},
    )
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("server_stopped", extra={"event": "server_stopped"})


def create_app() -> FastAPI:
    """Application factory. Tests build their own app via this function."""
    app = FastAPI(
        title="Stripe → Metronome Sidecar",
        version="0.1.0",
        lifespan=_lifespan,
        # We don't expose docs in production — the only public route is the
        # webhook and one health check. Flip these if you want them locally.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness check. Intentionally does not touch the DB.

        Load balancers / orchestrators hammer this endpoint; a DB roundtrip on
        every poll is a waste of connections. If you need a readiness probe
        that checks Postgres, add a separate ``/ready`` route.
        """
        return {"status": "ok"}

    # -------------------------------------------------------------------------
    # Webhook receiver
    # -------------------------------------------------------------------------

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request) -> JSONResponse:
        settings = get_settings()
        raw_body: bytes = await request.body()
        signature_header = request.headers.get("Stripe-Signature")

        try:
            event = verify_and_parse_event(
                payload=raw_body,
                signature_header=signature_header,
                webhook_secret=settings.stripe_webhook_secret.get_secret_value(),
            )
        except InvalidWebhookSignature as exc:
            logger.warning(
                "webhook_rejected",
                extra={
                    "event": "webhook_rejected",
                    "reason": "invalid_signature",
                    "detail": str(exc),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Stripe webhook signature.",
            ) from exc

        stripe_event_id = _require_str(event.get("id"), "id")
        event_type = _require_str(event.get("type"), "type")

        inserted = await _persist_event(
            stripe_event_id=stripe_event_id,
            event_type=event_type,
            payload=event,
        )

        logger.info(
            "webhook_received",
            extra={
                "event": "webhook_received",
                "stripe_event_id": stripe_event_id,
                "type": event_type,
                "persisted": inserted,
            },
        )
        return JSONResponse(
            content={"received": True, "stripe_event_id": stripe_event_id},
            status_code=status.HTTP_200_OK,
        )

    return app


# -----------------------------------------------------------------------------
# Persistence helpers — kept module-private to make the route function small.
# -----------------------------------------------------------------------------


async def _persist_event(
    *,
    stripe_event_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> bool:
    """Insert a webhook event idempotently.

    Returns
    -------
    bool
        ``True`` if a new row was inserted, ``False`` if a row with this
        ``stripe_event_id`` already existed (i.e. Stripe redelivered).
    """
    stmt = (
        pg_insert(WebhookEvent)
        .values(
            stripe_event_id=stripe_event_id,
            event_type=event_type,
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=[WebhookEvent.stripe_event_id])
        # `returning(...)` returns the inserted row if there *was* an insert
        # and nothing if the conflict path was taken — which is exactly the
        # signal we need to log "persisted vs duplicate".
        .returning(WebhookEvent.stripe_event_id)
    )

    async with session_scope() as session:
        result = await session.execute(stmt)
        inserted_row = result.scalar_one_or_none()

    return inserted_row is not None


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        # A valid Stripe-signed event always has these fields. If we're here,
        # something is wrong upstream — refuse the event so it can be re-sent
        # rather than persisting garbage.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Stripe event missing required field `{field_name}`.",
        )
    return value


# Module-level app instance for `uvicorn sidecar.server:app`.
app = create_app()
