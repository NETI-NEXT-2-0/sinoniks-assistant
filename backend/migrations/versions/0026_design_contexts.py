"""Pin immutable design context for execution and branch regeneration."""

import sqlalchemy as sa
from alembic import op

revision = "0026_design_contexts"
down_revision = "0025_design_revisions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_design_contexts",
        sa.Column("run_id", sa.UUID(), sa.ForeignKey("runs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("base_revision_id", sa.UUID(), sa.ForeignKey("design_revisions.id")),
    )


def downgrade():
    op.drop_table("run_design_contexts")
