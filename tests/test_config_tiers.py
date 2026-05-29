"""Unit tests for :mod:`sidecar.config.tiers`.

The tier config is a Python dict; these tests cover the two helpers that
operate on it (``lookup_tier`` and ``is_upgrade``) and assert the example
``TIERS`` content matches the README's documented shape.
"""

from __future__ import annotations

import pytest

from sidecar.config.tiers import (
    TIERS,
    Tier,
    UnknownTierError,
    is_upgrade,
    lookup_tier,
)


# These tests run against whatever ``TIERS`` is configured to in the fork —
# they intentionally do NOT hard-code price IDs because every customer of this
# repo edits ``tiers.py`` as part of onboarding. Hard-coded keys would mean
# the test suite fails for every fork the moment it gets configured.


def test_tiers_is_non_empty() -> None:
    """The fork must define at least one tier or the subscription handler
    has no work to do — every event would 4xx as ``UnknownTierError``.
    """
    assert TIERS, (
        "src/sidecar/config/tiers.py defines no tiers — add at least one "
        "Stripe price ID → Tier entry before deploying."
    )


def test_lookup_tier_returns_configured_tier() -> None:
    """A known Stripe price ID resolves to the matching Tier."""
    price_id, expected_tier = next(iter(TIERS.items()))
    tier = lookup_tier(price_id)
    assert isinstance(tier, Tier)
    assert tier == expected_tier


def test_lookup_tier_unknown_raises_with_helpful_message() -> None:
    """An unknown Stripe price ID raises ``UnknownTierError``, not ``KeyError``.

    The handler relies on the typed exception to classify the failure as
    permanent — bare ``KeyError`` would either bubble up as a 500 or get
    classified as transient by the worker's "unexpected exception" path.
    """
    with pytest.raises(UnknownTierError, match="price_does_not_exist"):
        lookup_tier("price_does_not_exist")


def test_is_upgrade_strict_inequality() -> None:
    """``is_upgrade`` checks ``new.rank > old.rank`` strictly.

    Same-rank transitions are not upgrades. v0.2b will treat them as no-ops
    (or as plan renames, depending on context), but the upgrade predicate
    itself stays simple. Built from inline ``Tier`` instances so the test
    doesn't depend on the fork having two tiers configured.
    """
    common_kwargs: dict[str, object] = {
        "credit_amount_per_period": 1,
        "metronome_credit_product_id": "p",
        "metronome_credit_type_id": "c",
        "recurrence_frequency": "MONTHLY",
    }
    low = Tier(name="low", rank=1, **common_kwargs)  # type: ignore[arg-type]
    high = Tier(name="high", rank=3, **common_kwargs)  # type: ignore[arg-type]

    assert is_upgrade(old_tier=low, new_tier=high) is True
    assert is_upgrade(old_tier=high, new_tier=low) is False
    assert is_upgrade(old_tier=low, new_tier=low) is False


def test_tiers_are_frozen_dataclasses() -> None:
    """Tier instances are frozen so accidentally mutating them is impossible.

    A handful of v0.2b flows pass tiers around by value; freezing them keeps
    that flow boring. Picks any tier from the configured ``TIERS`` so the
    test passes regardless of which keys the fork has set.
    """
    tier = next(iter(TIERS.values()))
    with pytest.raises((AttributeError, TypeError)):
        tier.name = "mutated"  # type: ignore[misc]
