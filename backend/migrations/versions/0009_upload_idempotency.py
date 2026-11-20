"""Bind retryable version admission to a document-scoped idempotency key."""

import sqlalchemy as sa
from alembic import op

revision = "0009_upload_idempotency"
down_revision = "0008_document_grants"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "document_versions", sa.Column("upload_idempotency_key", sa.Uuid(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_document_upload_key", "document_versions", ["document_id", "upload_idempotency_key"]
    )


def downgrade():
    op.drop_constraint("uq_document_upload_key", "document_versions", type_="unique")
    op.drop_column("document_versions", "upload_idempotency_key")
