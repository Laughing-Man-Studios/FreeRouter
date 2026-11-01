"""m2 quota state tables

Adds the four tables that close the CONSTITUTION 8.3 deviation recorded in
ADR-0004: persisted quota usage, cooldowns, model status, and per-credential
selection state.

Like the M0 tables, none of these stores prompt or completion text, and none
stores key material. `key_state` refers to a credential by alias only. A state
dump is therefore safe to read, diff and paste into an issue, which is the
point of keeping the schema this narrow.

The privacy constraint is encoded here rather than only in the application, so
it holds for every database built from this revision onwards.

Revision ID: 0002_quota_state
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_quota_state"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.create_table(
        "quota_windows",
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("window_seconds", sa.Float(), nullable=False),
        sa.Column("reset_kind", sa.String(), nullable=False),
        sa.Column("reset_at", sa.String(), nullable=True),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("flushed_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "model_id", "resource", "window_seconds"),
    )
    op.create_table(
        "cooldowns",
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("cooldown_until", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("retry_after", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "model_id"),
    )
    op.create_table(
        "model_status",
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "model_id"),
    )
    op.create_table(
        "key_state",
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("last_used_ordinal", sa.Integer(), nullable=False),
        sa.Column("suspended", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "alias"),
    )


def downgrade() -> None:
    """Revert this revision."""
    op.drop_table("key_state")
    op.drop_table("model_status")
    op.drop_table("cooldowns")
    op.drop_table("quota_windows")
