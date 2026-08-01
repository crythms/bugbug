"""Record who last edited a run action's params, and when.

Revision ID: a7d9c2e4f10b
Revises: f3c8a1d5b2e7
Create Date: 2026-08-01 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d9c2e4f10b"
down_revision: Union[str, Sequence[str], None] = "f3c8a1d5b2e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("run_actions", sa.Column("edited_by", sa.String(), nullable=True))
    op.add_column(
        "run_actions",
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("run_actions", "edited_at")
    op.drop_column("run_actions", "edited_by")
