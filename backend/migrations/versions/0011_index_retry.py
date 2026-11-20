"""Bound deterministic index retries and persist their schedule."""

import sqlalchemy as sa
from alembic import op

revision = "0011_index_retry"
down_revision = "0010_disclosure_context"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "document_versions",
        sa.Column("index_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "document_versions", sa.Column("index_retry_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_index_attempts", "document_versions", "index_attempts >= 0 AND index_attempts <= 5"
    )


def downgrade():
    op.drop_constraint("ck_index_attempts", "document_versions", type_="check")
    op.drop_column("document_versions", "index_retry_at")
    op.drop_column("document_versions", "index_attempts")
