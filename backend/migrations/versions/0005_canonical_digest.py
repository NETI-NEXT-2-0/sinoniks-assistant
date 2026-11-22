"""Bind reviewed canonical artifacts to a trusted database digest."""

import sqlalchemy as sa
from alembic import op

revision = "0005_canonical_digest"
down_revision = "0004_documents"
branch_labels = None
depends_on = None


def upgrade():
    # Existing artifacts cannot be authenticated retrospectively; re-ingest them.
    op.add_column("document_versions", sa.Column("canonical_sha256", sa.String(64)))


def downgrade():
    op.drop_column("document_versions", "canonical_sha256")
