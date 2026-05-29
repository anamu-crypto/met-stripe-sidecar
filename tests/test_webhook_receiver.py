"""Integration tests for the FastAPI webhook receiver.

These tests post real, signed-by-us payloads to ``/webhooks/stripe`` against
a real Postgres. The Stripe signature is computed inside the test (see
``make_stripe_signature`` in ``conftest.py``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sidecar.config import get_settings
from sidecar.models import WebhookEvent
from tests.conftest import make_stripe_signature


@pytest.fixture
def webhook_secret() -> str:
    return get_settings().stripe_webhook_secret.get_secret_value()


def _post_webhook(
    client: TestClient,
    *,
    payload: dict[str, Any],
    secret: str,
    signature_override: str | None = None,
) -> Any:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Stripe-Signature": signature_override or make_stripe_signature(body, secret),
        "Content-Type": "application/json",
    }
    # Pass raw bytes so signature verification sees identical bytes on both sides.
    return client.post("/webhooks/stripe", content=body, headers=headers)


def test_health_endpoint_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_valid_signature_persists_event(
    client: TestClient,
    webhook_secret: str,
    customer_created_event: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    response = _post_webhook(client, payload=customer_created_event, secret=webhook_secret)

    assert response.status_code == 200
    assert response.json()["received"] is True
    assert response.json()["stripe_event_id"] == customer_created_event["id"]

    # Row exists and contains the same payload we sent.
    count = await _count_events(db_session)
    assert count == 1

    row = await _get_event(db_session, customer_created_event["id"])
    assert row is not None
    assert row.event_type == "customer.created"
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.payload["data"]["object"]["id"] == "cus_TEST123"


@pytest.mark.asyncio
async def test_invalid_signature_returns_400(
    client: TestClient,
    customer_created_event: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    response = _post_webhook(
        client,
        payload=customer_created_event,
        secret="wrong-secret",
        signature_override=make_stripe_signature(
            json.dumps(customer_created_event).encode(), "wrong-secret"
        ),
    )
    assert response.status_code == 400

    # No row was inserted.
    assert await _count_events(db_session) == 0


@pytest.mark.asyncio
async def test_missing_signature_header_returns_400(
    client: TestClient,
    customer_created_event: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    body = json.dumps(customer_created_event).encode("utf-8")
    response = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert await _count_events(db_session) == 0


@pytest.mark.asyncio
async def test_duplicate_event_id_returns_200_without_inserting_new_row(
    client: TestClient,
    webhook_secret: str,
    customer_created_event: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    # First delivery: inserts.
    r1 = _post_webhook(client, payload=customer_created_event, secret=webhook_secret)
    assert r1.status_code == 200

    # Second delivery, same event id: still 200, still only one row.
    r2 = _post_webhook(client, payload=customer_created_event, secret=webhook_secret)
    assert r2.status_code == 200

    count = await _count_events(db_session)
    assert count == 1


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _count_events(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(WebhookEvent))
    return int(result.scalar_one())


async def _get_event(session: AsyncSession, stripe_event_id: str) -> WebhookEvent | None:
    stmt = select(WebhookEvent).where(WebhookEvent.stripe_event_id == stripe_event_id)
    return (await session.execute(stmt)).scalar_one_or_none()
