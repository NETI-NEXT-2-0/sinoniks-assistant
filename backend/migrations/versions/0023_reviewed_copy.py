"""Immutable curator-edited copy checksum, separate from source canonical."""

import sqlalchemy as sa
from alembic import op

revision = "0023_reviewed_copy"
down_revision = "0022_chat_files"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("document_versions", sa.Column("reviewed_copy_sha256", sa.String(64)))


def downgrade():
    op.drop_column("document_versions", "reviewed_copy_sha256")
