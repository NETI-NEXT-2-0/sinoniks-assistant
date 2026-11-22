"""Add an atomic daily model usage ledger."""
import sqlalchemy as sa
from alembic import op

revision = "0021_model_usage_daily"
down_revision = "0020_group_member_revision"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "model_usage_daily",
        sa.Column("usage_day", sa.DateTime(timezone=True), nullable=False),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("spent_usd_micros", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("request_count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("usage_day", "profile", "model"),
        sa.CheckConstraint("spent_usd_micros >= 0", name="ck_model_usage_spent"),
        sa.CheckConstraint("request_count >= 0", name="ck_model_usage_requests"),
    )

def downgrade():
    op.drop_table("model_usage_daily")
