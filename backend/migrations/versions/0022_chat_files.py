"""Private chat uploads and immutable per-run attachment selection."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_chat_files"
down_revision = "0021_model_usage_daily"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "chat_id", sa.Uuid(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("upload_key", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("original_key", sa.String(200), nullable=False, unique=True),
        sa.Column("canonical_sha256", sa.String(64)),
        sa.Column("chunks", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("warnings", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("chat_id", "upload_key", name="uq_chat_file_upload"),
        sa.CheckConstraint("status IN ('pending','ready','failed')", name="ck_chat_file_status"),
        sa.CheckConstraint("size_bytes > 0 AND size_bytes <= 26214400", name="ck_chat_file_size"),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_chat_file_attempts"),
    )
    op.create_index("ix_chat_files_chat_id", "chat_files", ["chat_id"])
    op.add_column(
        "runs", sa.Column("attachment_ids", postgresql.JSONB(), nullable=False, server_default="[]")
    )


def downgrade():
    op.drop_column("runs", "attachment_ids")
    op.drop_index("ix_chat_files_chat_id", table_name="chat_files")
    op.drop_table("chat_files")
