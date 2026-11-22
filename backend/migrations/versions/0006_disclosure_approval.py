"""Require explicit canonical approval before indexing or returning evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0006_disclosure_approval"
down_revision = "0005_canonical_digest"
branch_labels = None
depends_on = None


def upgrade():
    # Prior auto-selected units have no reliable approval record; never backfill.
    op.add_column("document_versions", sa.Column("approved_canonical_sha256", sa.String(64)))


def downgrade():
    op.drop_column("document_versions", "approved_canonical_sha256")
