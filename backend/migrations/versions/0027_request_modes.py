"""Persist explicit request purpose through queueing and branch regeneration."""

import sqlalchemy as sa
from alembic import op

revision = "0027_request_modes"
down_revision = "0026_design_contexts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column(
        "request_mode", sa.String(16), nullable=False, server_default="auto",
    ))
    op.create_check_constraint(
        "ck_run_request_mode", "runs", "request_mode IN ('auto','design','reference')",
    )


def downgrade():
    op.drop_constraint("ck_run_request_mode", "runs", type_="check")
    op.drop_column("runs", "request_mode")
