"""Persist parser attempt budget and next retry time."""

import sqlalchemy as sa
from alembic import op

revision = "0007_parser_retry"
down_revision = "0006_disclosure_approval"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "document_versions",
        sa.Column("parser_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "document_versions", sa.Column("parser_retry_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_parser_attempts", "document_versions", "parser_attempts >= 0 AND parser_attempts <= 5"
    )


def downgrade():
    op.drop_constraint("ck_parser_attempts", "document_versions", type_="check")
    op.drop_column("document_versions", "parser_retry_at")
    op.drop_column("document_versions", "parser_attempts")
