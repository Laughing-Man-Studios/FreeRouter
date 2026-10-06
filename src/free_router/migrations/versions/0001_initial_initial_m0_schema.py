"""initial m0 schema

Creates the two M0 tables from SQLAlchemy Core definitions: `models`, holding
the provider mapping, and `request_logs`, holding operational metadata.

`request_logs` deliberately has no prompt or completion column. That is a
privacy requirement (spec 9, CONSTITUTION 9.1), and encoding it in the initial
migration means the constraint holds for every database built from this
revision onwards.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.create_table(
        "models",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "request_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_id", sa.String(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_category", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("request_logs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_request_logs_request_id"), ["request_id"], unique=False
        )


def downgrade() -> None:
    """Revert this revision."""
    with op.batch_alter_table("request_logs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_request_logs_request_id"))

    op.drop_table("request_logs")
    op.drop_table("models")
