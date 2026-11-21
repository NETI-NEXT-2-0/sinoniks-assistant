"""Voluntary product feedback, isolated from private conversation contents."""

import sqlalchemy as sa
from alembic import op

revision = "0030_product_feedback"
down_revision = "0029_render_timings"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "product_feedback",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "chat_id", sa.UUID(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "run_id", sa.UUID(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("author", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_feedback_rating"),
        sa.UniqueConstraint("chat_id"),
    )


def downgrade():
    op.drop_table("product_feedback")
