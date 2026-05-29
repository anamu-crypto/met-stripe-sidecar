"""Handler-level exception types.

Handlers raise from this module so the worker only needs to know two things:

  - :class:`RetryableHandlerError` — schedule a backoff and try again.
  - :class:`PermanentHandlerError` — give up; mark the row ``failed``.

Anything else bubbling up the call stack is treated by the worker as
"unexpected → retry" (safer default; failing loudly is preferable to giving
up too eagerly).

These types are intentionally narrow. The classification work — translating
Metronome 5xx vs 4xx, mapper errors, missing customer mappings, etc. — lives
in each handler module. Handlers are the layer that knows enough about the
business to make a retry decision; the worker is not.
"""

from __future__ import annotations


class HandlerError(Exception):
    """Base class for handler-level failures.

    Catch this in tests that want to assert "the handler raised something
    classified" without caring which side of the retry split it landed on.
    """


class RetryableHandlerError(HandlerError):
    """The handler couldn't complete, but the same event may succeed later.

    Examples:
      - Metronome returned 5xx or timed out.
      - A ``customer.subscription.created`` event arrived before the
        corresponding ``customer.created`` was processed, so the customer
        mapping doesn't exist yet.

    The worker schedules an exponential-backoff retry. After
    ``WORKER_MAX_ATTEMPTS`` attempts, the row is marked ``failed`` and the
    worker logs ``event_failed_exhausted``.
    """


class PermanentHandlerError(HandlerError):
    """The same event will fail the same way next time. Don't retry.

    Examples:
      - The Stripe payload is malformed (missing required field, wrong type).
      - The Stripe price ID isn't configured in ``TIERS``.
      - Metronome returned 4xx (other than 429) — semantic rejection by the
        API.

    The worker marks the row ``failed`` immediately and logs
    ``event_failed_permanent``.
    """


__all__ = ["HandlerError", "PermanentHandlerError", "RetryableHandlerError"]
