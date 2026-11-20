"""Record the disclosure rendering contract without fabricating old context."""

import sqlalchemy as sa
from alembic import op

revision = "0010_disclosure_context"
down_revision = "0009_upload_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("document_versions", sa.Column("disclosure_format", sa.String(32), nullable=True))


def downgrade():
    op.drop_column("document_versions", "disclosure_format")
