"""Track group membership changes for consistent curator pagination."""
import sqlalchemy as sa
from alembic import op

revision = "0020_group_member_revision"
down_revision = "0019_group_membership_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "groups", sa.Column("membership_revision", sa.BigInteger(), nullable=False,
                            server_default="1")
    )
    op.create_check_constraint(
        "ck_group_membership_revision", "groups", "membership_revision >= 1"
    )


def downgrade():
    op.drop_constraint("ck_group_membership_revision", "groups", type_="check")
    op.drop_column("groups", "membership_revision")
