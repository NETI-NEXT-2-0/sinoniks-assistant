"""Add direct-user answer denies at disclosure-unit granularity."""

import sqlalchemy as sa
from alembic import op

revision = "0013_unit_denies"
down_revision = "0012_text_retry"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "disclosure_answer_denies",
        sa.Column(
            "unit_id", sa.Uuid(), sa.ForeignKey("disclosure_units.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
    )
    op.create_index("ix_disclosure_answer_denies_user_id", "disclosure_answer_denies", ["user_id"])


def downgrade():
    op.drop_index("ix_disclosure_answer_denies_user_id", table_name="disclosure_answer_denies")
    op.drop_table("disclosure_answer_denies")
