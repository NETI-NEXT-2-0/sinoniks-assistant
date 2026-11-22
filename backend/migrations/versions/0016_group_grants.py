"""Bind documents explicitly to workspaces and grant groups individual actions."""
import sqlalchemy as sa
from alembic import op

revision = "0016_group_grants"
down_revision = "0015_acl_history_guard"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("workspace_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_documents_workspace", "documents", "workspaces", ["workspace_id"], ["id"]
    )
    op.create_unique_constraint("uq_document_workspace", "documents", ["id", "workspace_id"])
    op.create_unique_constraint("uq_group_workspace", "groups", ["id", "workspace_id"])
    op.create_table(
        "document_group_grants",
        sa.Column("document_id", sa.Uuid(), primary_key=True),
        sa.Column("group_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("allow_answer", sa.Boolean(), nullable=False),
        sa.Column("allow_original", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "workspace_id"], ["documents.id", "documents.workspace_id"]
        ),
        sa.ForeignKeyConstraint(["group_id", "workspace_id"], ["groups.id", "groups.workspace_id"]),
        sa.CheckConstraint("allow_answer OR allow_original", name="ck_group_grant_nonempty"),
    )
    op.create_index("ix_document_group_grants_group_id", "document_group_grants", ["group_id"])


def downgrade():
    op.drop_index("ix_document_group_grants_group_id", table_name="document_group_grants")
    op.drop_table("document_group_grants")
    op.drop_constraint("uq_group_workspace", "groups", type_="unique")
    op.drop_constraint("uq_document_workspace", "documents", type_="unique")
    op.drop_constraint("fk_documents_workspace", "documents", type_="foreignkey")
    op.drop_column("documents", "workspace_id")
