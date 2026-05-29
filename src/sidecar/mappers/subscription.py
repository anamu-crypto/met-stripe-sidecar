"""Stripe subscription object → Metronome ``POST /v1/contracts/create`` body.

Companion to :mod:`sidecar.mappers.customer`. Same rules apply: pure function,
no I/O, no clock reads, no globals. Pure functions are how this sidecar stays
trivially testable and easy to customize — change the dict that comes out of
here and you change exactly what Metronome receives, with no spooky action at
a distance.

Default mapping
---------------
For a typical "Stripe handles billing, Metronome handles usage allotment" PLG
setup, the defaults below produce a contract that:

  * Starts at the Stripe subscription's ``current_period_start`` (Unix
    seconds → RFC 3339 UTC).
  * Names the contract ``"Stripe Subscription {sub_id} ({tier_name})"`` so
    Metronome operators can correlate without leaving the dashboard.
  * Sets a deterministic ``uniqueness_key`` derived from
    ``{sub_id}_{price_id}_{period_start}`` so re-delivery of the same Stripe
    webhook never produces a duplicate contract (Metronome returns 409 on
    collision — see :mod:`sidecar.handlers.subscription` for recovery).
  * Configures the billing provider as Stripe with delivery method
    ``direct_to_billing_provider`` — matches v0.1's customer mapper.
  * Attaches exactly one recurring credit sized to ``tier.credit_amount_per_period``,
    denominated in ``tier.metronome_credit_type_id``, recurring at
    ``tier.recurrence_frequency``, with ``rollover_fraction=1.0`` so a future
    v0.2b ``SUPERSEDE`` transition (rollover-on-upgrade) is possible without a
    backfill.

Assumptions baked in
--------------------
1. Single-item subscriptions only. A multi-item Stripe subscription raises
   ``ValueError`` so it's caught at the API boundary, not silently dropped.
2. Stripe billing cadence matches ``tier.recurrence_frequency``. If your
   business needs them to differ (e.g. annual Stripe billing with monthly
   credit drops) override the ``recurrence_frequency`` field below.
3. ``current_period_start`` may live at the subscription level (Stripe API
   ≤ 2024-12-01) or on the item (Stripe API ≥ 2025-03-31).
   :func:`_period_start_unix` reads from the item first and falls back to the
   subscription level, so the mapper works on both API versions without an
   environment flag. If neither location has a value, the mapper raises.

Hour-boundary flooring
----------------------
Metronome rejects recurring credits whose ``starting_at`` is not on an hour
boundary (e.g. ``1:00`` is accepted, ``1:30`` is not). Stripe's
``current_period_start`` is the exact second the subscription was created, so
the mapper floors it down to the hour before emitting both ``starting_at``
and the ``uniqueness_key``. Effect: a contract may begin up to ~59 minutes
before the actual Stripe subscription start. For credit-allotment use cases
this is fine; if you need exact alignment you must round up to the next hour
*and* update the Stripe subscription's ``billing_cycle_anchor`` to match —
override :func:`_period_start_unix` if you do.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sidecar.config.tiers import Tier


def stripe_subscription_to_metronome_contract_request(
    stripe_subscription: dict[str, Any],
    tier: Tier,
    metronome_customer_id: str,
    rate_card_id: str,
) -> dict[str, Any]:
    """Translate a Stripe subscription into a Metronome contract create request.

    Parameters
    ----------
    stripe_subscription:
        The ``data.object`` from a ``customer.subscription.created`` webhook.
        This is a Stripe Subscription object, *not* the wrapping Event.
    tier:
        The :class:`~sidecar.config.tiers.Tier` resolved from the
        subscription's single price ID via
        :func:`~sidecar.config.tiers.lookup_tier`.
    metronome_customer_id:
        The Metronome customer UUID from ``customer_mappings``.
    rate_card_id:
        UUID of the Metronome rate card to bind the contract to. Comes from
        ``Settings.metronome_default_rate_card_id``.

    Returns
    -------
    dict
        Request body for ``POST /v1/contracts/create`` (the Metronome SDK
        accepts it as ``**kwargs``).

    Raises
    ------
    ValueError
        - ``stripe_subscription`` has anything other than exactly one item.
        - Required fields (``id``, item ``price.id``, ``current_period_start``)
          are missing.

        The handler maps ``ValueError`` to ``PermanentHandlerError`` — a
        malformed payload will not become well-formed on retry.

    Notes
    -----
    Pure function: no I/O, no globals, no side effects, no clock reads.
    """
    subscription_id = _require_str(stripe_subscription.get("id"), "id")
    price_id, item = _require_single_item(stripe_subscription)
    period_start_unix = _period_start_unix(stripe_subscription, item)

    starting_at_iso = _unix_to_iso_utc(period_start_unix)

    # Deterministic key: the same Stripe webhook, redelivered or replayed,
    # always produces the same uniqueness_key, which is what lets Metronome
    # reject duplicates with 409 and lets the handler recover the existing
    # contract ID instead of creating a second one.
    uniqueness_key = f"{subscription_id}_{price_id}_{period_start_unix}"

    # CUSTOMIZE: contract display name. Whatever appears here surfaces in the
    # Metronome UI; the default makes the Stripe ↔ Metronome correlation
    # obvious to an operator without leaving the dashboard.
    contract_name = f"Stripe Subscription {subscription_id} ({tier.name})"

    # CUSTOMIZE: billing-provider configuration on the contract. Mirrors the
    # default in mappers/customer.py — every contract bills through Stripe.
    billing_provider_configuration: dict[str, Any] = {
        "billing_provider": "stripe",
        "delivery_method": "direct_to_billing_provider",
    }

    # CUSTOMIZE: the recurring credit shape. The defaults below produce one
    # recurring credit per contract, sized to the tier's per-period allotment.
    # If you want multiple credits per tier (e.g. "10M events AND $500 USD
    # cushion") this is the place — append more dicts to ``recurring_credits``
    # and extend ``Tier`` with the extra fields you need.
    recurring_credit: dict[str, Any] = {
        "name": f"{tier.name} {tier.recurrence_frequency.lower()} allotment",
        "product_id": tier.metronome_credit_product_id,
        "priority": 100,
        "starting_at": starting_at_iso,
        "access_amount": {
            "credit_type_id": tier.metronome_credit_type_id,
            "quantity": tier.credit_amount_per_period,
            "unit_price": 1,
        },
        "commit_duration": {
            "value": 1,
            "unit": "PERIODS",
        },
        # See module docstring on rollover_fraction=1.0: preserves the option
        # to roll over unspent balance on a future tier transition. The
        # decision to *actually* roll over lives on the v0.2b transition.
        "rollover_fraction": 1.0,
        "recurrence_frequency": tier.recurrence_frequency,
    }

    request: dict[str, Any] = {
        "customer_id": metronome_customer_id,
        "rate_card_id": rate_card_id,
        "starting_at": starting_at_iso,
        "uniqueness_key": uniqueness_key,
        "name": contract_name,
        "billing_provider_configuration": billing_provider_configuration,
        "recurring_credits": [recurring_credit],
    }

    return request


# -----------------------------------------------------------------------------
# Internal helpers. Kept at module scope so the public mapper stays a thin
# top-to-bottom read.
# -----------------------------------------------------------------------------


def _require_str(value: Any, field_path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"Stripe subscription field `{field_path}` is missing or not a string."
        )
    return value


def _require_single_item(stripe_subscription: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(price_id, item)`` for the one item on the subscription.

    Raises ``ValueError`` if the subscription has zero items or more than one.
    Multi-item subscriptions are explicitly out of scope for v0.2a — splitting
    a multi-item subscription into multiple contracts would change the
    semantics of "one Stripe sub ↔ one Metronome contract" that v0.2a relies
    on. Surface the rejection rather than silently picking the first item.
    """
    items = stripe_subscription.get("items")
    if not isinstance(items, dict):
        raise ValueError("Stripe subscription is missing `items`.")

    data = items.get("data")
    if not isinstance(data, list):
        raise ValueError("Stripe subscription `items.data` is not a list.")
    if len(data) == 0:
        raise ValueError("Stripe subscription has no items; refusing to map.")
    if len(data) > 1:
        raise ValueError(
            f"Stripe subscription has {len(data)} items; multi-item "
            "subscriptions are not supported in v0.2a."
        )

    item = data[0]
    if not isinstance(item, dict):
        raise ValueError("Stripe subscription `items.data[0]` is not an object.")

    price = item.get("price")
    if not isinstance(price, dict):
        raise ValueError("Stripe subscription item is missing `price`.")
    price_id = _require_str(price.get("id"), "items.data[0].price.id")
    return price_id, item


def _period_start_unix(
    stripe_subscription: dict[str, Any], item: dict[str, Any]
) -> int:
    """Return ``current_period_start`` as a Unix timestamp, floored to the hour.

    Read order: item-level first, then subscription-level fallback. Stripe API
    versions ≤ 2024-12-01 expose ``current_period_start`` at the subscription
    top level; ≥ 2025-03-31 the field moved onto each subscription item.
    Reading both lets the same code run against both API versions without an
    environment flag — useful for forks that pin different Stripe API versions
    in different environments.

    The returned value is **floored to the nearest hour** because Metronome
    rejects recurring credits whose ``starting_at`` is not on an hour boundary
    (see module docstring for the trade-off). The hour-floor is applied here,
    in one place, so the mapper body stays a thin top-to-bottom read.
    """
    raw = item.get("current_period_start")
    if not isinstance(raw, int):
        raw = stripe_subscription.get("current_period_start")
    if not isinstance(raw, int):
        raise ValueError(
            "Stripe subscription field `current_period_start` is missing or "
            "not an integer Unix timestamp on either the subscription or the "
            "item — cannot determine the contract start time."
        )
    return raw - (raw % 3600)


def _unix_to_iso_utc(ts_unix: int) -> str:
    """Convert a Unix timestamp to an RFC 3339 UTC string Metronome accepts.

    Locked behavior: the format ends with ``+00:00`` (not ``Z``); the
    Metronome SDK accepts either, but we standardize on ``+00:00`` to match
    Python's stdlib output and make the mapper tests deterministic.
    """
    return datetime.fromtimestamp(ts_unix, tz=UTC).isoformat()
