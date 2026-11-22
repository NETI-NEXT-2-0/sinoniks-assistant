"""Optimistic revisions for operator-managed accounts."""

import sqlalchemy as sa
from alembic import op

revision = "0003_identity_admin"
down_revision = "0002_chat_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("revision", sa.Integer(), server_default="1", nullable=False))
    op.create_check_constraint("ck_user_revision", "users", "revision >= 1")


def downgrade() -> None:
    op.drop_constraint("ck_user_revision", "users", type_="check")
    op.drop_column("users", "revision")
