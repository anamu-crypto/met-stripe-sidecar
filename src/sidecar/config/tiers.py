"""Tier configuration: Stripe price IDs → Metronome credit allotments.

Why this lives in code
----------------------
A "tier" (Startup, Step Up, Higher, etc.) is *business-wide* configuration: the
allotment that comes with each tier is the same for every customer who picks
that tier. So we keep it in this Python module — versioned with the codebase
and reviewable in pull requests — rather than stuffing it into Stripe price
metadata (which is per-price-object and easy to drift between environments) or
Metronome custom fields (which are per-customer and would require a backfill
on every change).

When you launch a new tier you add an entry. When you change an allotment you
update an entry. Either change ships through your normal review and deploy
process.

Field-by-field reference
------------------------
- ``name``: short human label used in contract names and logs (e.g. "startup").
- ``rank``: ordinal used by :func:`is_upgrade` to decide whether one tier is
  "higher" than another. Reserved for v0.2b (upgrade/downgrade handling); not
  used during v0.2a contract creation. Set it to a strictly increasing integer
  in price order so the comparison works the moment v0.2b ships.
- ``credit_amount_per_period``: how much credit drops each period, in units of
  ``metronome_credit_type_id`` (USD cents, events, CPU-seconds, tokens, …).
- ``metronome_credit_product_id``: UUID of the Metronome product the credit
  draws against. You set products up in Metronome before configuring tiers.
- ``metronome_credit_type_id``: UUID of the Metronome credit type the
  allotment is denominated in. Most businesses use a single credit type
  across tiers (e.g. USD cents), but the field is per-tier so you can mix
  denominations if you need to.
- ``recurrence_frequency``: how often the credit drops. One of
  ``MONTHLY``, ``QUARTERLY``, ``ANNUAL``, ``WEEKLY``. The Metronome API
  accepts all four values verbatim.

Assumption
----------
Stripe billing cadence is assumed to match ``recurrence_frequency`` (i.e. if
the Stripe price bills monthly, the credit drops monthly). If your business
needs them to differ — e.g. Stripe bills annually but credit drops monthly —
override the recurrence in ``mappers/subscription.py`` rather than relying on
this default. See the README "Tier configuration" section.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RecurrenceFrequency = Literal["MONTHLY", "QUARTERLY", "ANNUAL", "WEEKLY"]


@dataclass(frozen=True, slots=True)
class Tier:
    """One pricing tier and what it grants the customer in Metronome."""

    name: str
    rank: int
    credit_amount_per_period: int
    metronome_credit_product_id: str
    metronome_credit_type_id: str
    recurrence_frequency: RecurrenceFrequency


class UnknownTierError(Exception):
    """Raised when a Stripe price ID is not configured in :data:`TIERS`.

    The handler treats this as a permanent failure: an unknown price ID means
    either (a) a tier was launched on the Stripe side without a corresponding
    change to this config (the usual cause — fix the config), or (b) the
    subscription is for a product this sidecar isn't supposed to mirror to
    Metronome at all (rare; filter on the receiver side if so).
    """

    def __init__(self, stripe_price_id: str) -> None:
        super().__init__(
            f"Stripe price ID {stripe_price_id!r} is not configured in "
            f"sidecar.config.tiers.TIERS. Add an entry there and redeploy."
        )
        self.stripe_price_id = stripe_price_id


# =============================================================================
# CUSTOMIZE: replace the example tiers below with the ones your business sells.
#
# Each key is a Stripe price ID. Each value is the Metronome side of that tier.
# Add a row for every Stripe price that should provision a Metronome contract;
# delete the example rows once you've added your own.
#
# The three example tiers below intentionally span different recurrence
# frequencies (monthly + annual) and different credit amounts to make the
# shape obvious. Real values will be specific to your Metronome setup —
# get the product and credit-type UUIDs from your Metronome dashboard.
# =============================================================================

TIERS: dict[str, Tier] = {
    "price_REPLACE_ME_startup": Tier(
        name="startup",
        rank=1,
        credit_amount_per_period=10_000_000,
        metronome_credit_product_id="REPLACE_ME_startup_credit_product_uuid",
        metronome_credit_type_id="REPLACE_ME_credit_type_uuid",
        recurrence_frequency="MONTHLY",
    ),
    "price_REPLACE_ME_stepup": Tier(
        name="step_up",
        rank=2,
        credit_amount_per_period=100_000_000,
        metronome_credit_product_id="REPLACE_ME_stepup_credit_product_uuid",
        metronome_credit_type_id="REPLACE_ME_credit_type_uuid",
        recurrence_frequency="MONTHLY",
    ),
    "price_REPLACE_ME_higher_annual": Tier(
        name="higher",
        rank=3,
        credit_amount_per_period=200_000_000,
        metronome_credit_product_id="REPLACE_ME_higher_credit_product_uuid",
        metronome_credit_type_id="REPLACE_ME_credit_type_uuid",
        recurrence_frequency="ANNUAL",
    ),
}


def lookup_tier(stripe_price_id: str) -> Tier:
    """Return the :class:`Tier` configured for ``stripe_price_id``.

    Raises
    ------
    UnknownTierError
        If no tier is configured for the price ID. The handler maps this to a
        permanent failure.
    """
    try:
        return TIERS[stripe_price_id]
    except KeyError as exc:
        raise UnknownTierError(stripe_price_id) from exc


def is_upgrade(old_tier: Tier, new_tier: Tier) -> bool:
    """Return ``True`` iff ``new_tier`` is ranked strictly above ``old_tier``.

    Reserved for v0.2b's upgrade/downgrade logic, where an upgrade may keep
    the existing credit balance via Metronome's ``SUPERSEDE`` transition while
    a downgrade does not. Defined here in the config module so the rank
    semantics live next to the data they operate on.
    """
    return new_tier.rank > old_tier.rank


__all__ = [
    "TIERS",
    "RecurrenceFrequency",
    "Tier",
    "UnknownTierError",
    "is_upgrade",
    "lookup_tier",
]
