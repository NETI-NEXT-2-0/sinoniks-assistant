"""Immutable Sinoniks drafts and exports bound to their chat branch."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_design_revisions"
down_revision = "0024_web_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "design_revisions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "chat_id", sa.UUID(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("branch_id", sa.UUID(), nullable=False),
        sa.Column("parent_id", sa.UUID(), sa.ForeignKey("design_revisions.id")),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.UUID(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("artifacts", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["branch_id", "chat_id"], ["chat_branches.id", "chat_branches.chat_id"],
            ondelete="CASCADE", name="fk_design_branch_chat",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_design_revision"),
        sa.UniqueConstraint("branch_id", "revision", name="uq_design_branch_revision"),
        sa.UniqueConstraint("chat_id", "idempotency_key", name="uq_design_request_key"),
    )
    op.create_index("ix_design_revisions_chat_id", "design_revisions", ["chat_id"])
    op.create_index("ix_design_revisions_branch_id", "design_revisions", ["branch_id"])


def downgrade():
    op.drop_table("design_revisions")
