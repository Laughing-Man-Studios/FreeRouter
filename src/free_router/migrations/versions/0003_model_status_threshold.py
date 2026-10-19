"""model status threshold

Adds ``threshold_tokens`` to ``model_status``.

The breaker degrades a model for requests *at or above* a threshold, not for
requests in general: a model that has only been shown to fail on a 900k-token
request must still serve a 4k-token one. The threshold is therefore the whole
meaning of the degraded state, and without a column for it the state cannot be
restored -- a restart would either forget the degradation entirely or, worse,
degrade the model without knowing which requests it applies to.

``detail`` could have carried this as text, but a value that has to be parsed
back out of a string is a value that will eventually be parsed wrongly. The
column costs a migration; a parsing bug costs a wrong quota decision.

Revision ID: 0003_model_status_threshold
Revises: 0002_quota_state
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_model_status_threshold"
down_revision: str | None = "0002_quota_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    with op.batch_alter_table("model_status", schema=None) as batch_op:
        batch_op.add_column(sa.Column("threshold_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Revert this revision."""
    with op.batch_alter_table("model_status", schema=None) as batch_op:
        batch_op.drop_column("threshold_tokens")
