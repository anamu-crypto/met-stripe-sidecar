"""Unit tests for :mod:`sidecar.mappers.customer`.

Pure-function tests — no fixtures beyond literal dicts.
"""

from __future__ import annotations

import pytest

from sidecar.mappers.customer import MapperError, stripe_customer_to_metronome_request


def test_minimal_payload_with_name_only() -> None:
    """A Stripe customer with just `id` and `name` maps to a valid Metronome body."""
    out = stripe_customer_to_metronome_request({"id": "cus_min", "name": "Minimal Co"})

    assert out["name"] == "Minimal Co"
    assert out["ingest_aliases"] == ["cus_min"]
    cfg = out["customer_billing_provider_configurations"][0]
    assert cfg["billing_provider"] == "stripe"
    assert cfg["delivery_method"] == "direct_to_billing_provider"
    assert cfg["configuration"]["stripe_customer_id"] == "cus_min"
    assert cfg["configuration"]["stripe_collection_method"] == "charge_automatically"


def test_full_payload_uses_name_not_email() -> None:
    """When both `name` and `email` are present, `name` wins."""
    stripe_customer = {
        "id": "cus_full",
        "name": "Acme, Inc.",
        "email": "billing@acme.example",
        "description": "ignored",
        "metadata": {"internal_id": "x"},
    }
    out = stripe_customer_to_metronome_request(stripe_customer)
    assert out["name"] == "Acme, Inc."
    assert out["ingest_aliases"] == ["cus_full"]


def test_missing_name_falls_back_to_email() -> None:
    """When `name` is missing or empty, `email` becomes the display name."""
    stripe_customer = {"id": "cus_noname", "name": "", "email": "lead@example.com"}
    out = stripe_customer_to_metronome_request(stripe_customer)
    assert out["name"] == "lead@example.com"


def test_missing_name_and_email_raises() -> None:
    """A customer with no name *and* no email cannot be created in Metronome."""
    with pytest.raises(MapperError, match="neither `name` nor `email`"):
        stripe_customer_to_metronome_request({"id": "cus_anon"})


def test_extra_fields_are_ignored() -> None:
    """Unmapped Stripe fields don't appear in the Metronome request body."""
    stripe_customer = {
        "id": "cus_extra",
        "name": "Has Extras",
        "balance": 1000,
        "currency": "usd",
        "discount": None,
        "shipping": {"name": "ship-to"},
        "address": {"country": "US"},
    }
    out = stripe_customer_to_metronome_request(stripe_customer)
    # Keys that exist
    assert set(out.keys()) == {
        "name",
        "ingest_aliases",
        "customer_billing_provider_configurations",
    }
    # Stripe-only fields didn't leak through
    for forbidden in ("balance", "currency", "discount", "shipping", "address"):
        assert forbidden not in out


def test_missing_id_raises() -> None:
    """Defensive: a malformed payload without `id` is a hard error."""
    with pytest.raises(MapperError, match="missing a string `id`"):
        stripe_customer_to_metronome_request({"name": "No ID Co"})


def test_whitespace_only_name_falls_back_to_email() -> None:
    """A name that's only whitespace should not be accepted as a real name."""
    out = stripe_customer_to_metronome_request(
        {"id": "cus_ws", "name": "   ", "email": "real@example.com"}
    )
    assert out["name"] == "real@example.com"
