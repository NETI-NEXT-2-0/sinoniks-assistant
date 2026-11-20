"""Add group-level answer denies on version-bound disclosure units."""
import sqlalchemy as sa
from alembic import op

revision = "0017_group_unit_denies"
down_revision = "0016_group_grants"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "disclosure_group_denies",
        sa.Column("unit_id", sa.Uuid(), sa.ForeignKey("disclosure_units.id"), primary_key=True),
        sa.Column("group_id", sa.Uuid(), sa.ForeignKey("groups.id"), primary_key=True),
    )
    op.create_index("ix_disclosure_group_denies_group_id", "disclosure_group_denies", ["group_id"])


def downgrade():
    op.drop_index("ix_disclosure_group_denies_group_id", table_name="disclosure_group_denies")
    op.drop_table("disclosure_group_denies")
