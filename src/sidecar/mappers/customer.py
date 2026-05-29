"""Stripe customer object → Metronome `POST /v1/customers` request body.

This is the most important file in the repo for customers forking it. The
default mapping below is the minimum required to make Metronome's
Stripe-as-billing-provider integration work end-to-end.

Any line tagged `# CUSTOMIZE:` is intentionally a customer-editable seam.
Common customizations:

  - mapping Stripe `metadata` keys onto Metronome `custom_fields`
  - adding extra `ingest_aliases` (e.g. an internal account ID)
  - changing the Stripe collection method to `send_invoice`
  - choosing `aws_marketplace`, `azure_marketplace`, etc. as the billing provider

Keep this function pure: no DB calls, no Metronome API calls, no time.now().
That property is what makes the rest of the codebase easy to reason about.
"""

from __future__ import annotations

from typing import Any


class MapperError(ValueError):
    """Raised when a Stripe payload cannot be mapped to a Metronome request.

    Treated as a *permanent* failure by the handler (no retries) because the
    same payload will fail the same way next time.
    """


def stripe_customer_to_metronome_request(stripe_customer: dict[str, Any]) -> dict[str, Any]:
    """Translate a Stripe customer object into the body for ``POST /v1/customers``.

    Parameters
    ----------
    stripe_customer:
        The ``data.object`` from a ``customer.created`` or ``customer.updated``
        webhook. This is a Stripe Customer object, *not* the wrapping Event.

    Returns
    -------
    dict
        Request body for Metronome's ``client.v1.customers.create(...)`` /
        ``POST /v1/customers``. Always contains at least ``name`` and
        ``ingest_aliases``.

    Raises
    ------
    MapperError
        If neither ``name`` nor ``email`` is present on the Stripe customer —
        Metronome requires a non-empty display name.
    """
    stripe_customer_id = stripe_customer.get("id")
    if not stripe_customer_id or not isinstance(stripe_customer_id, str):
        raise MapperError("Stripe customer payload is missing a string `id`.")

    name = _resolve_display_name(stripe_customer)

    # CUSTOMIZE: which Stripe IDs should become Metronome ingest aliases.
    # By default we use just the Stripe customer ID, which is what lets usage
    # events sent with `customer_id=cus_xxx` reach the right Metronome customer.
    ingest_aliases: list[str] = [stripe_customer_id]

    # CUSTOMIZE: extra billing-provider configuration. The defaults below set up
    # the canonical "Stripe is the billing provider, Metronome pushes charges
    # directly" topology. Read https://docs.metronome.com if you need a different
    # collection method or provider.
    billing_provider_config: dict[str, Any] = {
        "billing_provider": "stripe",
        "delivery_method": "direct_to_billing_provider",
        "configuration": {
            "stripe_customer_id": stripe_customer_id,
            "stripe_collection_method": "charge_automatically",
        },
    }

    request: dict[str, Any] = {
        "name": name,
        "ingest_aliases": ingest_aliases,
        "customer_billing_provider_configurations": [billing_provider_config],
    }

    # CUSTOMIZE: forward Stripe metadata to Metronome custom_fields.
    # We do NOT do this by default because Metronome custom_fields require
    # keys to be pre-registered via the dashboard. Uncomment + edit if you've
    # registered keys.
    #
    # metadata = stripe_customer.get("metadata") or {}
    # if metadata:
    #     request["custom_fields"] = {
    #         f"stripe_{k}": v for k, v in metadata.items() if v is not None
    #     }

    return request


def _resolve_display_name(stripe_customer: dict[str, Any]) -> str:
    """Return a non-empty name for Metronome, falling back to email if needed.

    Mapping rule (in order of preference):
      1. ``name`` if present and non-empty.
      2. ``email`` if present and non-empty.
      3. Raise ``MapperError`` — Metronome rejects empty names.
    """
    raw_name = stripe_customer.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        return raw_name.strip()

    raw_email = stripe_customer.get("email")
    if isinstance(raw_email, str) and raw_email.strip():
        return raw_email.strip()

    raise MapperError(
        "Stripe customer has neither `name` nor `email`; cannot map to a "
        "Metronome customer name."
    )
