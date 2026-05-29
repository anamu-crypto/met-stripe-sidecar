"""Structured JSON logging setup.

Every log line is a single JSON object. This makes the output trivial to ship
into Datadog, Splunk, Loki, CloudWatch, etc. without parser configuration.

Always log with `extra={...}` to attach structured context, e.g.

    logger.info(
        "handler_completed",
        extra={
            "event": "handler_completed",
            "stripe_event_id": event_id,
            "stripe_customer_id": cust_id,
            "metronome_customer_id": metronome_id,
            "outcome": "ok",
            "duration_ms": elapsed_ms,
        },
    )

Avoid putting variable data in the log *message*; keep messages short and put
data in the extra fields. This keeps logs easy to filter and group by message.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from pythonjsonlogger.json import JsonFormatter


class _SidecarJsonFormatter(JsonFormatter):
    """JSON formatter that always includes `level`, `logger`, and `timestamp`.

    We override `add_fields` to normalize field names (`timestamp` rather than
    `asctime`, lowercased `level`) so the wire format is stable across releases
    of python-json-logger.
    """

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record.setdefault("timestamp", self.formatTime(record, self.datefmt))
        log_record["level"] = record.levelname.lower()
        log_record["logger"] = record.name


_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: safe to call from both the FastAPI app and the worker entrypoint.
    """
    global _CONFIGURED
    if _CONFIGURED:
        # Update level only; do not duplicate handlers.
        logging.getLogger().setLevel(level.upper())
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        _SidecarJsonFormatter(
            "%(timestamp)s %(level)s %(logger)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S.%fZ",
        )
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # Quiet down noisy third-party loggers; let our app logger speak.
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper so callers don't import the stdlib `logging` module."""
    return logging.getLogger(name)
