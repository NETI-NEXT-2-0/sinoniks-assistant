"""Persist bounded retries for UTF-8 document preparation."""

import sqlalchemy as sa
from alembic import op

revision = "0012_text_retry"
down_revision = "0011_index_retry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "document_versions",
        sa.Column("text_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "document_versions", sa.Column("text_retry_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_text_attempts", "document_versions", "text_attempts >= 0 AND text_attempts <= 5"
    )


def downgrade():
    op.drop_constraint("ck_text_attempts", "document_versions", type_="check")
    op.drop_column("document_versions", "text_retry_at")
    op.drop_column("document_versions", "text_attempts")
