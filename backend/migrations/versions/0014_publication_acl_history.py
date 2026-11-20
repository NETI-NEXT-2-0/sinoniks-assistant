"""Preserve the prior publication when active publication is withdrawn."""

import sqlalchemy as sa
from alembic import op

revision = "0014_publication_acl"
down_revision = "0013_unit_denies"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("last_published_version_id", sa.Uuid(), nullable=True))
    op.execute("UPDATE documents SET last_published_version_id = active_version_id")
    op.create_foreign_key(
        "fk_document_last_published_version", "documents", "document_versions",
        ["last_published_version_id", "id"], ["id", "document_id"],
        ondelete="SET NULL (last_published_version_id)",
    )


def downgrade():
    op.drop_constraint("fk_document_last_published_version", "documents", type_="foreignkey")
    op.drop_column("documents", "last_published_version_id")
