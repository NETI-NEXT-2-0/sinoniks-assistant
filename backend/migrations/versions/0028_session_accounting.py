"""Per-call accounting independent of run ownership and daily budget reservations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028_session_accounting"
down_revision = "0027_request_modes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_calls",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("runs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(200)),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("response_id", sa.String(200)),
        sa.Column("tariff", JSONB(), nullable=False),
        sa.Column("usage", JSONB(), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.CheckConstraint("status IN ('started','succeeded','failed','cancelled')",
                           name="ck_model_call_status"),
    )
    op.create_index("ix_model_calls_run_id", "model_calls", ["run_id"])
    op.create_table(
        "turn_timings",
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("runs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("elapsed_ms >= 0", name="ck_turn_timing_nonnegative"),
    )


def downgrade():
    op.drop_table("turn_timings")
    op.drop_table("model_calls")
