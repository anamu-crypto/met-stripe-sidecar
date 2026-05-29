"""Event handlers — the orchestration layer between mappers and Metronome.

A handler:
  1. Pulls the data.object out of a Stripe event,
  2. Calls the appropriate mapper to build a Metronome request,
  3. Calls the Metronome client,
  4. Persists the resulting Stripe-id ↔ Metronome-id mapping.

Handlers are *not* pure: they touch the DB and the Metronome API. They are
where retry-vs-fail classification turns into a verdict the worker can act on.

Each handler module owns its own ``HandlerOutcome`` enum and ``HandlerResult``
dataclass because the relevant outcomes diverge between customer and
subscription flows (e.g. subscription has ``CREATED_VIA_UNIQUENESS_KEY_RECOVERY``
that doesn't apply to customers). The worker doesn't care — it discards the
return value — but tests and structured logs benefit from the precision.
"""

from sidecar.handlers.customer import (
    handle_customer_created,
    handle_customer_updated,
)
from sidecar.handlers.errors import (
    HandlerError,
    PermanentHandlerError,
    RetryableHandlerError,
)
from sidecar.handlers.subscription import (
    handle_subscription_created,
    handle_subscription_deleted,
    handle_subscription_updated,
)

__all__ = [
    "HandlerError",
    "PermanentHandlerError",
    "RetryableHandlerError",
    "handle_customer_created",
    "handle_customer_updated",
    "handle_subscription_created",
    "handle_subscription_deleted",
    "handle_subscription_updated",
]
