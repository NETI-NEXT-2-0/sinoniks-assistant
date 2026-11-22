"""Optional provenance for curator-uploaded snapshots of public pages."""

import sqlalchemy as sa
from alembic import op

revision = "0024_web_snapshots"
down_revision = "0023_reviewed_copy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("document_versions", sa.Column("source_url", sa.String(2048)))
    op.add_column("document_versions", sa.Column("source_captured_on", sa.Date()))
    op.create_check_constraint(
        "ck_version_web_source_pair", "document_versions",
        "(source_url IS NULL) = (source_captured_on IS NULL)",
    )


def downgrade():
    op.drop_constraint("ck_version_web_source_pair", "document_versions", type_="check")
    op.drop_column("document_versions", "source_captured_on")
    op.drop_column("document_versions", "source_url")
