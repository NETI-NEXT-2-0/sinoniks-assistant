"""Add explicit per-user document grants and a separate ACL revision."""

import sqlalchemy as sa
from alembic import op

revision = "0008_document_grants"
down_revision = "0007_parser_retry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "documents", sa.Column("acl_revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_check_constraint("ck_document_acl_revision", "documents", "acl_revision >= 1")
    op.create_table(
        "document_grants",
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("allow_answer", sa.Boolean(), nullable=False),
        sa.Column("allow_original", sa.Boolean(), nullable=False),
        sa.CheckConstraint("allow_answer OR allow_original", name="ck_document_grant_nonempty"),
    )
    op.create_index("ix_document_grants_user_id", "document_grants", ["user_id"])


def downgrade():
    op.drop_index("ix_document_grants_user_id", table_name="document_grants")
    op.drop_table("document_grants")
    op.drop_constraint("ck_document_acl_revision", "documents", type_="check")
    op.drop_column("documents", "acl_revision")
