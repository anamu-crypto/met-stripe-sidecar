"""Runtime configuration.

All configuration comes from environment variables. We use Pydantic's
`BaseSettings` so we get type-checked parsing, a single import-time validation
point, and trivially mockable settings in tests.

Customers forking this repo will primarily edit:

  - environment variables (see `.env.example`)
  - the tier-to-credit mapping in `sidecar/config/tiers.py`
  - the mapper functions in `sidecar/mappers/` (clearly marked with
    `# CUSTOMIZE:` comments)

If you need to add a new configuration value, declare it here so it is
documented in one place and type-checked the same way as everything else.

This module is also a Python package (it has a `tiers` submodule) so the
canonical imports are:

    from sidecar.config import Settings, get_settings
    from sidecar.config.tiers import TIERS, lookup_tier
"""

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# RFC 4122-style UUID. Metronome's product / credit-type / rate-card identifiers
# are all UUIDs, and the SDK forwards whatever string we pass — so an obviously-
# bogus value (the placeholder in `.env.example`, a comment, a typo) only fails
# at the Metronome API boundary as a 400. Validating shape at process startup
# turns that into a clear configuration error instead.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All `SecretStr` fields are masked when the settings object is repr'd or
    logged, so secrets never leak into structured logs or error reports
    accidentally.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ----- Required -----------------------------------------------------------

    stripe_webhook_secret: SecretStr = Field(
        ...,
        description="Stripe webhook signing secret (whsec_...). Used to verify "
        "every incoming webhook signature.",
    )

    metronome_api_key: SecretStr = Field(
        ...,
        description="Bearer token for the Metronome API.",
    )

    database_url: str = Field(
        ...,
        description=(
            "SQLAlchemy async URL for Postgres, e.g. "
            "`postgresql+asyncpg://user:pass@host:5432/db`."
        ),
    )

    metronome_default_rate_card_id: str = Field(
        ...,
        description=(
            "Metronome rate card UUID used for every contract created by this "
            "sidecar. Every v0.2a subscription contract is bound to this rate "
            "card; per-customer overrides are out of scope. Find it under "
            "Rate Cards in the Metronome dashboard."
        ),
    )

    @field_validator("metronome_default_rate_card_id")
    @classmethod
    def _require_uuid(cls, value: str) -> str:
        """Reject placeholder / non-UUID rate card IDs at process startup.

        Without this, an unconfigured ``METRONOME_DEFAULT_RATE_CARD_ID`` (e.g.
        the literal ``REPLACE_ME_with_rate_card_uuid`` from ``.env.example``)
        sails through to the Metronome API and surfaces as a 400 on the first
        ``customer.subscription.created`` event — long after the operator has
        moved on. Failing fast keeps the misconfiguration adjacent to the
        deploy that introduced it.
        """
        if not _UUID_RE.match(value):
            raise ValueError(
                f"METRONOME_DEFAULT_RATE_CARD_ID={value!r} is not a UUID. "
                "Set it to the rate card UUID from your Metronome dashboard "
                "(Rate Cards → copy ID); the placeholder shipped in "
                "`.env.example` will be rejected by the Metronome API."
            )
        return value

    # ----- Optional -----------------------------------------------------------

    log_level: str = Field(default="INFO", description="Python log level.")

    worker_poll_interval_seconds: float = Field(
        default=2.0,
        ge=0.1,
        description="How long the worker sleeps when it finds no pending work.",
    )

    worker_max_attempts: int = Field(
        default=5,
        ge=1,
        description="Maximum number of times the worker will try an event "
        "before marking it `failed`.",
    )

    worker_retry_base_seconds: float = Field(
        default=30.0,
        ge=1.0,
        description="Base delay for exponential backoff between retries.",
    )

    worker_retry_cap_seconds: float = Field(
        default=3600.0,
        ge=1.0,
        description="Maximum delay between retries, regardless of attempt count.",
    )

    metronome_base_url: str = Field(
        default="https://api.metronome.com",
        description="Base URL for the Metronome API.",
    )

    port: int = Field(default=8000, ge=1, le=65535, description="HTTP listen port.")

    # ----- Convenience --------------------------------------------------------

    @property
    def sync_database_url(self) -> str:
        """Synchronous variant of the DB URL, used by Alembic."""
        return self.database_url.replace("+asyncpg", "+psycopg2").replace(
            "postgresql+psycopg2",
            "postgresql",
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-singleton `Settings` instance.

    Cached so repeated calls do not re-read the environment. Tests can override
    by clearing the cache: `get_settings.cache_clear()`.
    """
    return Settings()  # type: ignore[call-arg]


__all__ = ["Settings", "get_settings"]
