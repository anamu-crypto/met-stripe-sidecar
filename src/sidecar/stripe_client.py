"""Thin wrapper around Stripe's webhook signature verification.

We deliberately keep this module tiny: the only Stripe-SDK surface we use is
``stripe.Webhook.construct_event``. That function:

  - verifies the HMAC signature in the ``Stripe-Signature`` header against the
    raw request body using our webhook signing secret,
  - parses the JSON payload, and
  - returns a ``stripe.Event`` object (or raises if invalid).

By isolating this behind a function we can test the receiver without standing
up the real Stripe SDK in a particular way, and a customer who wants to swap
in a custom verifier (e.g. dual-secret rotation) has one obvious place to do it.
"""

from __future__ import annotations

import json
from typing import Any

import stripe
from stripe import SignatureVerificationError


class InvalidWebhookSignature(Exception):
    """Raised when a webhook signature cannot be verified.

    Wrapping the Stripe SDK's exception lets the receiver layer be coupled to
    *our* exception, not Stripe's, which makes the receiver easier to mock and
    means a future SDK update can't silently change our error contract.
    """


def verify_and_parse_event(
    payload: bytes,
    signature_header: str | None,
    webhook_secret: str,
) -> dict[str, Any]:
    """Verify a Stripe webhook signature and return the parsed event as a dict.

    Parameters
    ----------
    payload:
        The raw, undecoded request body. Stripe's signature is over the bytes
        on the wire — decoding to ``str`` and re-encoding can change them
        (e.g. line endings) and invalidate the signature.
    signature_header:
        The value of the ``Stripe-Signature`` header. ``None`` is treated as
        invalid.
    webhook_secret:
        The signing secret from Stripe (``whsec_...``).

    Returns
    -------
    dict
        The Stripe Event payload as a plain ``dict`` (not a ``stripe.Event``)
        so downstream code does not depend on the Stripe SDK's object model.

    Raises
    ------
    InvalidWebhookSignature
        If the signature is missing, malformed, expired, or does not match.
    """
    if not signature_header:
        raise InvalidWebhookSignature("Missing Stripe-Signature header.")

    try:
        # `construct_event` verifies the HMAC signature AND parses the payload.
        # We discard its returned `stripe.Event` and parse the bytes ourselves
        # below — that avoids depending on the SDK's internal serializers (whose
        # methods have moved between releases) and keeps the rest of the system
        # holding nothing but builtin types.
        stripe.Webhook.construct_event(  # type: ignore[no-untyped-call]
            payload=payload,
            sig_header=signature_header,
            secret=webhook_secret,
        )
    except SignatureVerificationError as exc:
        raise InvalidWebhookSignature(str(exc)) from exc
    except ValueError as exc:
        # Raised when payload is not valid JSON. Treat as a signature problem
        # at the API boundary — the caller responds 400 either way.
        raise InvalidWebhookSignature(f"Malformed webhook payload: {exc}") from exc

    # Signature is valid; safe to deserialise.
    try:
        result: dict[str, Any] = json.loads(payload)
    except json.JSONDecodeError as exc:
        # Shouldn't happen — construct_event would have raised — but defensive.
        raise InvalidWebhookSignature(f"Malformed webhook payload: {exc}") from exc

    if not isinstance(result, dict):
        raise InvalidWebhookSignature("Webhook payload is not a JSON object.")
    return result
