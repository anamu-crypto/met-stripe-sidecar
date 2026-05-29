"""add subscription_mappings (v0.2a)

Revision ID: 0002_subscription_mappings
Revises: 0001_initial
Create Date: 2026-05-14 00:00:00.000000

Adds the ``subscription_mappings`` table linking a Stripe subscription to a
Metronome contract, and a partial index on ``stripe_customer_id`` for the
operator query "list all subscriptions for a Stripe customer".

The foreign key to ``customer_mappings.stripe_customer_id`` enforces the
invariant that a subscription mapping cannot exist before its parent customer
mapping does — same shape as v0.1's customer linkage, applied to subscriptions.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_subscription_mappings"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscription_mappings",
        sa.Column("stripe_subscription_id", sa.Text(), nullable=False),
        sa.Column("stripe_customer_id", sa.Text(), nullable=False),
        sa.Column("metronome_contract_id", sa.Text(), nullable=False),
        sa.Column("current_tier_name", sa.Text(), nullable=False),
        sa.Column("current_stripe_price_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("stripe_subscription_id"),
        sa.UniqueConstraint(
            "metronome_contract_id",
            name="uq_subscription_mappings_metronome_contract_id",
        ),
        sa.ForeignKeyConstraint(
            ["stripe_customer_id"],
            ["customer_mappings.stripe_customer_id"],
            name="fk_subscription_mappings_stripe_customer",
        ),
    )

    op.create_index(
        "idx_subscription_mappings_stripe_customer",
        "subscription_mappings",
        ["stripe_customer_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_subscription_mappings_stripe_customer",
        table_name="subscription_mappings",
    )
    op.drop_table("subscription_mappings")
