"""Identity foundation, intentionally before chats and document migrations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    timestamp = sa.DateTime(timezone=True)
    op.execute("CREATE SCHEMA IF NOT EXISTS langgraph")
    op.create_table(
        "users",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("normalized_login", sa.String(128), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.CheckConstraint("role IN ('user', 'curator', 'admin')"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("user_id", uuid, sa.ForeignKey("users.id")),
        sa.Column("created_at", timestamp, nullable=False),
        sa.Column("last_seen_at", timestamp, nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("absolute_expires_at", timestamp, nullable=False),
        sa.Column("revoked_at", timestamp),
        sa.CheckConstraint("expires_at <= absolute_expires_at"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_table(
        "workspaces",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
    )
    op.create_table(
        "memberships",
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("user_id", uuid, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
    )
    op.create_table(
        "groups",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
    )
    op.create_table(
        "group_members",
        sa.Column("group_id", uuid, sa.ForeignKey("groups.id"), primary_key=True),
        sa.Column("user_id", uuid, sa.ForeignKey("users.id"), primary_key=True),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("actor_id", uuid, sa.ForeignKey("users.id")),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128)),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
    )
    op.create_table(
        "rate_limits",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("window_start", timestamp, primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("count >= 0"),
    )
    op.create_index("ix_rate_limit_window", "rate_limits", ["window_start"])


def downgrade() -> None:
    for table in (
        "rate_limits",
        "audit_events",
        "group_members",
        "groups",
        "memberships",
        "workspaces",
        "sessions",
        "users",
    ):
        op.drop_table(table)
    # langgraph is managed by its own provision step and may contain checkpoints.
