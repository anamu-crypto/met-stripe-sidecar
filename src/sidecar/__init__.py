"""Stripe → Metronome sidecar sync service.

This package contains the receiver (FastAPI) and worker (polling loop) for
keeping Metronome customers in sync with Stripe customers via webhooks.
"""

__version__ = "0.1.0"
