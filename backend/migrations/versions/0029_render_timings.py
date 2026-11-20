"""Separate complete render observations from historical card timings."""

import sqlalchemy as sa
from alembic import op

revision = "0029_render_timings"
down_revision = "0028_session_accounting"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "turn_render_timings",
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("runs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("elapsed_ms >= 0", name="ck_full_timing_nonnegative"),
    )


def downgrade():
    op.drop_table("turn_render_timings")
