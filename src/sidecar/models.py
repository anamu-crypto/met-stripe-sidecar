"""SQLAlchemy ORM models.

These mirror the Alembic migration in `alembic/versions/0001_initial.py`. If
you change a column here, you MUST add a new Alembic migration — do not edit
existing migrations and do not rely on `Base.metadata.create_all` for schema
management in production.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# -----------------------------------------------------------------------------
# Status values are stored as TEXT (not a Postgres ENUM) so adding a new state
# is a no-op migration. Keep the canonical set documented here.
# -----------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"  # reserved for future use; current impl uses row locks
STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"

ALL_STATUSES = frozenset({STATUS_PENDING, STATUS_PROCESSING, STATUS_PROCESSED, STATUS_FAILED})


class WebhookEvent(Base):
    """A Stripe webhook event we have persisted but may not yet have processed.

    Primary key is `stripe_event_id` — that gives us idempotency for free at the
    receiver layer via `INSERT ... ON CONFLICT DO NOTHING`.
    """

    __tablename__ = "webhook_events"

    stripe_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Partial index on the worker's query path: pending + ready to run.
        Index(
            "idx_webhook_events_pending",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"WebhookEvent(stripe_event_id={self.stripe_event_id!r}, "
            f"event_type={self.event_type!r}, status={self.status!r}, "
            f"attempts={self.attempts})"
        )


class CustomerMapping(Base):
    """A confirmed mapping between a Stripe customer and a Metronome customer.

    A row in this table is the source of truth that the customer exists in
    both systems and the two IDs are linked. The handler creates this row only
    after Metronome confirms the customer has been created.
    """

    __tablename__ = "customer_mappings"

    stripe_customer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    metronome_customer_id: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
        # Note: we update `updated_at` explicitly in the handler rather than
        # relying on a Postgres trigger, to keep the schema portable.
    )

    def __repr__(self) -> str:
        return (
            f"CustomerMapping(stripe_customer_id={self.stripe_customer_id!r}, "
            f"metronome_customer_id={self.metronome_customer_id!r})"
        )


class SubscriptionMapping(Base):
    """A confirmed mapping between a Stripe subscription and a Metronome contract.

    Like ``CustomerMapping``, a row here is the source of truth that the
    contract exists in Metronome and the two IDs are linked. The handler
    writes the row only *after* Metronome confirms the contract has been
    created (or, in the 409 recovery path, after Metronome confirms the
    contract was already created in a previous attempt).

    The foreign key to ``customer_mappings`` enforces "the customer mapping
    must exist before the subscription mapping" at the database level —
    matching v0.1's invariant that all customer linkage is durable before any
    subscription linkage points at it.

    ``current_tier_name`` / ``current_stripe_price_id`` are denormalised here
    for two reasons:
      1. They let operators answer "what tier is this subscription on?"
         without joining against Stripe.
      2. They give v0.2b (upgrade/downgrade) the diff it needs to detect a
         tier change cheaply: read the old tier from this row, look up the
         new tier from the webhook, compare.
    """

    __tablename__ = "subscription_mappings"

    stripe_subscription_id: Mapped[str] = mapped_column(Text, primary_key=True)
    stripe_customer_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("customer_mappings.stripe_customer_id"),
        nullable=False,
    )
    metronome_contract_id: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    current_tier_name: Mapped[str] = mapped_column(Text, nullable=False)
    current_stripe_price_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
        # Note: as with CustomerMapping.updated_at, we set this from the
        # handler rather than via a Postgres trigger to keep the schema
        # portable. v0.2b will bump this on tier transitions.
    )

    __table_args__ = (
        # Most operator queries are "show me all subscriptions for this
        # Stripe customer" — partial-by-customer scan is the hot path.
        Index(
            "idx_subscription_mappings_stripe_customer",
            "stripe_customer_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"SubscriptionMapping(stripe_subscription_id={self.stripe_subscription_id!r}, "
            f"metronome_contract_id={self.metronome_contract_id!r}, "
            f"current_tier_name={self.current_tier_name!r})"
        )
