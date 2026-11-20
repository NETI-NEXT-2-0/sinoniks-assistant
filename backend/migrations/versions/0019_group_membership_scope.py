"""Require group members to belong to the group's workspace."""
import sqlalchemy as sa
from alembic import op

revision = "0019_group_membership_scope"
down_revision = "0018_group_deny_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("group_members", sa.Column("workspace_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE group_members AS gm
        SET workspace_id = g.workspace_id
        FROM groups AS g
        JOIN memberships AS m ON m.workspace_id = g.workspace_id
        WHERE gm.group_id = g.id AND gm.user_id = m.user_id
    """)
    op.alter_column("group_members", "workspace_id", nullable=False)
    op.drop_constraint("group_members_group_id_fkey", "group_members", type_="foreignkey")
    op.drop_constraint("group_members_user_id_fkey", "group_members", type_="foreignkey")
    op.create_foreign_key(
        "fk_group_member_group_workspace", "group_members", "groups",
        ["group_id", "workspace_id"], ["id", "workspace_id"],
    )
    op.create_foreign_key(
        "fk_group_member_workspace_membership", "group_members", "memberships",
        ["workspace_id", "user_id"], ["workspace_id", "user_id"],
    )


def downgrade():
    op.drop_constraint("fk_group_member_workspace_membership", "group_members", type_="foreignkey")
    op.drop_constraint("fk_group_member_group_workspace", "group_members", type_="foreignkey")
    op.create_foreign_key(
        "group_members_group_id_fkey", "group_members", "groups", ["group_id"], ["id"]
    )
    op.create_foreign_key(
        "group_members_user_id_fkey", "group_members", "users", ["user_id"], ["id"]
    )
    op.drop_column("group_members", "workspace_id")
