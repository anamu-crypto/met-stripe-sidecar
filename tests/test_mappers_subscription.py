"""Unit tests for :mod:`sidecar.mappers.subscription`.

Pure-function tests — no DB, no HTTP, no clock. Each test constructs a Stripe
subscription dict (in the v0.2a-supported single-item shape) and asserts the
resulting Metronome contract request body matches the spec.
"""

from __future__ import annotations

from typing import Any

import pytest

from sidecar.config.tiers import Tier
from sidecar.mappers.subscription import (
    stripe_subscription_to_metronome_contract_request,
)

# -----------------------------------------------------------------------------
# Fixtures (literal dicts so the tests read top-to-bottom).
# -----------------------------------------------------------------------------

# 2026-01-01T00:00:00+00:00 — Thursday — readable when it shows up in
# assertions, so test failures are easy to scan.
_PERIOD_START_UNIX = 1_767_225_600
_PERIOD_START_ISO = "2026-01-01T00:00:00+00:00"

_STARTUP_TIER = Tier(
    name="startup",
    rank=1,
    credit_amount_per_period=10_000_000,
    metronome_credit_product_id="prod_startup_uuid",
    metronome_credit_type_id="credit_type_events_uuid",
    recurrence_frequency="MONTHLY",
)

_HIGHER_TIER = Tier(
    name="higher",
    rank=3,
    credit_amount_per_period=200_000_000,
    metronome_credit_product_id="prod_higher_uuid",
    metronome_credit_type_id="credit_type_events_uuid",
    recurrence_frequency="ANNUAL",
)

_METRONOME_CUSTOMER_ID = "11111111-1111-1111-1111-111111111111"
_RATE_CARD_ID = "22222222-2222-2222-2222-222222222222"


def _single_item_subscription(
    *,
    sub_id: str = "sub_test_123",
    price_id: str = "price_REPLACE_ME_startup",
    period_start: int = _PERIOD_START_UNIX,
    status: str = "active",
    extra_item_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimum-viable Stripe subscription dict for these tests."""
    item: dict[str, Any] = {
        "id": "si_test_1",
        "price": {"id": price_id},
    }
    if extra_item_fields:
        item.update(extra_item_fields)
    return {
        "id": sub_id,
        "status": status,
        "current_period_start": period_start,
        "items": {
            "object": "list",
            "data": [item],
        },
    }


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_minimum_viable_monthly_subscription() -> None:
    """Happy path: a single-item active monthly subscription produces the expected body."""
    sub = _single_item_subscription()

    out = stripe_subscription_to_metronome_contract_request(
        stripe_subscription=sub,
        tier=_STARTUP_TIER,
        metronome_customer_id=_METRONOME_CUSTOMER_ID,
        rate_card_id=_RATE_CARD_ID,
    )

    # Top-level shape
    assert out["customer_id"] == _METRONOME_CUSTOMER_ID
    assert out["rate_card_id"] == _RATE_CARD_ID
    assert out["starting_at"] == _PERIOD_START_ISO
    assert out["uniqueness_key"] == (
        f"sub_test_123_price_REPLACE_ME_startup_{_PERIOD_START_UNIX}"
    )
    assert out["name"] == "Stripe Subscription sub_test_123 (startup)"

    # Billing provider
    assert out["billing_provider_configuration"] == {
        "billing_provider": "stripe",
        "delivery_method": "direct_to_billing_provider",
    }

    # Recurring credit (single)
    assert len(out["recurring_credits"]) == 1
    credit = out["recurring_credits"][0]
    assert credit == {
        "name": "startup monthly allotment",
        "product_id": "prod_startup_uuid",
        "priority": 100,
        "starting_at": _PERIOD_START_ISO,
        "access_amount": {
            "credit_type_id": "credit_type_events_uuid",
            "quantity": 10_000_000,
            "unit_price": 1,
        },
        "commit_duration": {"value": 1, "unit": "PERIODS"},
        "rollover_fraction": 1.0,
        "recurrence_frequency": "MONTHLY",
    }


def test_trial_subscription_maps_same_shape() -> None:
    """A trialing subscription is treated as active: credit flows immediately.

    The shape of the request body should not depend on ``status``; v0.2a
    intentionally has no trial-end-specific logic.
    """
    active = stripe_subscription_to_metronome_contract_request(
        stripe_subscription=_single_item_subscription(status="active"),
        tier=_STARTUP_TIER,
        metronome_customer_id=_METRONOME_CUSTOMER_ID,
        rate_card_id=_RATE_CARD_ID,
    )
    trialing = stripe_subscription_to_metronome_contract_request(
        stripe_subscription=_single_item_subscription(status="trialing"),
        tier=_STARTUP_TIER,
        metronome_customer_id=_METRONOME_CUSTOMER_ID,
        rate_card_id=_RATE_CARD_ID,
    )

    assert active == trialing


def test_multi_item_subscription_raises() -> None:
    """A multi-item Stripe subscription is out of scope for v0.2a."""
    sub = _single_item_subscription()
    sub["items"]["data"].append({"id": "si_test_2", "price": {"id": "price_other"}})

    with pytest.raises(ValueError, match="multi-item"):
        stripe_subscription_to_metronome_contract_request(
            stripe_subscription=sub,
            tier=_STARTUP_TIER,
            metronome_customer_id=_METRONOME_CUSTOMER_ID,
            rate_card_id=_RATE_CARD_ID,
        )


def test_missing_current_period_start_raises() -> None:
    """The mapper refuses to guess a start time if Stripe didn't send one."""
    sub = _single_item_subscription()
    del sub["current_period_start"]

    with pytest.raises(ValueError, match="current_period_start"):
        stripe_subscription_to_metronome_contract_request(
            stripe_subscription=sub,
            tier=_STARTUP_TIER,
            metronome_customer_id=_METRONOME_CUSTOMER_ID,
            rate_card_id=_RATE_CARD_ID,
        )


def test_uniqueness_key_is_deterministic() -> None:
    """The same input must produce the same uniqueness_key on every call.

    This is the property Metronome's 409 dedup relies on: re-delivery of the
    same Stripe webhook (or a worker retry after a partial failure) must hit
    the same key so Metronome rejects the duplicate instead of creating a
    second contract.
    """
    sub = _single_item_subscription()

    first = stripe_subscription_to_metronome_contract_request(
        stripe_subscription=sub,
        tier=_STARTUP_TIER,
        metronome_customer_id=_METRONOME_CUSTOMER_ID,
        rate_card_id=_RATE_CARD_ID,
    )
    second = stripe_subscription_to_metronome_contract_request(
        stripe_subscription=sub,
        tier=_STARTUP_TIER,
        metronome_customer_id=_METRONOME_CUSTOMER_ID,
        rate_card_id=_RATE_CARD_ID,
    )

    assert first["uniqueness_key"] == second["uniqueness_key"]
    # And it's the deterministic concatenation, not a randomized UUID.
    assert first["uniqueness_key"] == (
        f"sub_test_123_price_REPLACE_ME_startup_{_PERIOD_START_UNIX}"
    )


def test_unix_timestamp_converts_to_expected_iso_string() -> None:
    """Lock the exact ISO 8601 format the mapper produces (``+00:00`` suffix).

    Metronome accepts both ``Z`` and ``+00:00`` but we standardize on the
    Python-stdlib output. If this assertion ever changes, downstream
    consumers of ``starting_at`` (logs, debugging, the operator copy-paste
    workflow) change with it — that should be a deliberate decision.
    """
    sub = _single_item_subscription(period_start=1_767_225_600)
    out = stripe_subscription_to_metronome_contract_request(
        stripe_subscription=sub,
        tier=_STARTUP_TIER,
        metronome_customer_id=_METRONOME_CUSTOMER_ID,
        rate_card_id=_RATE_CARD_ID,
    )
    assert out["starting_at"] == "2026-01-01T00:00:00+00:00"
    assert out["recurring_credits"][0]["starting_at"] == "2026-01-01T00:00:00+00:00"


def test_annual_tier_produces_annual_recurrence_and_credit_name() -> None:
    """A non-monthly tier's recurrence_frequency must flow through verbatim."""
    sub = _single_item_subscription(price_id="price_REPLACE_ME_higher_annual")

    out = stripe_subscription_to_metronome_contract_request(
        stripe_subscription=sub,
        tier=_HIGHER_TIER,
        metronome_customer_id=_METRONOME_CUSTOMER_ID,
        rate_card_id=_RATE_CARD_ID,
    )

    credit = out["recurring_credits"][0]
    assert credit["recurrence_frequency"] == "ANNUAL"
    assert credit["name"] == "higher annual allotment"
    assert credit["product_id"] == "prod_higher_uuid"
    assert credit["access_amount"]["quantity"] == 200_000_000


def test_missing_id_raises() -> None:
    """Defensive: a payload without a subscription `id` is a hard error."""
    sub = _single_item_subscription()
    del sub["id"]
    with pytest.raises(ValueError, match="`id`"):
        stripe_subscription_to_metronome_contract_request(
            stripe_subscription=sub,
            tier=_STARTUP_TIER,
            metronome_customer_id=_METRONOME_CUSTOMER_ID,
            rate_card_id=_RATE_CARD_ID,
        )


def test_missing_price_id_raises() -> None:
    """Defensive: a subscription item without `price.id` is a hard error."""
    sub = _single_item_subscription()
    sub["items"]["data"][0]["price"] = {}
    with pytest.raises(ValueError, match="price.id"):
        stripe_subscription_to_metronome_contract_request(
            stripe_subscription=sub,
            tier=_STARTUP_TIER,
            metronome_customer_id=_METRONOME_CUSTOMER_ID,
            rate_card_id=_RATE_CARD_ID,
        )


def test_period_start_falls_back_to_item_when_subscription_level_missing() -> None:
    """Stripe API ≥ 2025-03-31 puts ``current_period_start`` on the item.

    The mapper must read item-first, subscription-second, so the same code
    works against both API versions without an environment flag.
    """
    sub = _single_item_subscription()
    del sub["current_period_start"]
    sub["items"]["data"][0]["current_period_start"] = _PERIOD_START_UNIX

    out = stripe_subscription_to_metronome_contract_request(
        stripe_subscription=sub,
        tier=_STARTUP_TIER,
        metronome_customer_id=_METRONOME_CUSTOMER_ID,
        rate_card_id=_RATE_CARD_ID,
    )

    assert out["starting_at"] == _PERIOD_START_ISO
    assert out["uniqueness_key"].endswith(f"_{_PERIOD_START_UNIX}")


def test_off_hour_period_start_is_floored_to_hour() -> None:
    """Metronome rejects non-hour-aligned ``starting_at`` on recurring credits.

    The mapper floors ``current_period_start`` down to the nearest hour so
    ``starting_at`` is always on an hour boundary; a Stripe subscription
    created at 12:34:56 ends up with a 12:00:00 contract start. Lock the
    behavior here so a future "round to nearest hour" or "ceil to hour"
    refactor is a deliberate decision, not an accident.
    """
    sub = _single_item_subscription(
        period_start=_PERIOD_START_UNIX + 30 * 60 + 17  # +30m17s
    )

    out = stripe_subscription_to_metronome_contract_request(
        stripe_subscription=sub,
        tier=_STARTUP_TIER,
        metronome_customer_id=_METRONOME_CUSTOMER_ID,
        rate_card_id=_RATE_CARD_ID,
    )

    assert out["starting_at"] == _PERIOD_START_ISO  # floored to the hour
    assert out["recurring_credits"][0]["starting_at"] == _PERIOD_START_ISO
    # uniqueness_key is built from the floored timestamp, so a Stripe
    # redelivery seconds later still produces the same key.
    assert out["uniqueness_key"].endswith(f"_{_PERIOD_START_UNIX}")
