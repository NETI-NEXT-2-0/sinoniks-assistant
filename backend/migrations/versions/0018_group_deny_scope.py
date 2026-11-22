"""Enforce the group-deny unit/version/document/workspace chain in PostgreSQL."""

import sqlalchemy as sa
from alembic import op

revision = "0018_group_deny_scope"
down_revision = "0017_group_unit_denies"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("disclosure_group_denies", sa.Column("version_id", sa.Uuid(), nullable=True))
    op.add_column("disclosure_group_denies", sa.Column("document_id", sa.Uuid(), nullable=True))
    op.add_column("disclosure_group_denies", sa.Column("workspace_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE disclosure_group_denies AS d
        SET version_id = u.version_id, document_id = v.document_id, workspace_id = doc.workspace_id
        FROM disclosure_units AS u
        JOIN document_versions AS v ON v.id = u.version_id
        JOIN documents AS doc ON doc.id = v.document_id
        JOIN groups AS g ON g.workspace_id = doc.workspace_id
        WHERE d.unit_id = u.id AND d.group_id = g.id
    """)
    for column in ("version_id", "document_id", "workspace_id"):
        op.alter_column("disclosure_group_denies", column, nullable=False)
    op.create_unique_constraint(
        "uq_disclosure_unit_version", "disclosure_units", ["id", "version_id"]
    )
    op.drop_constraint(
        "disclosure_group_denies_unit_id_fkey", "disclosure_group_denies", type_="foreignkey"
    )
    op.drop_constraint(
        "disclosure_group_denies_group_id_fkey", "disclosure_group_denies", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_group_deny_unit_version",
        "disclosure_group_denies",
        "disclosure_units",
        ["unit_id", "version_id"],
        ["id", "version_id"],
    )
    op.create_foreign_key(
        "fk_group_deny_version_document",
        "disclosure_group_denies",
        "document_versions",
        ["version_id", "document_id"],
        ["id", "document_id"],
    )
    op.create_foreign_key(
        "fk_group_deny_document_workspace",
        "disclosure_group_denies",
        "documents",
        ["document_id", "workspace_id"],
        ["id", "workspace_id"],
    )
    op.create_foreign_key(
        "fk_group_deny_group_workspace",
        "disclosure_group_denies",
        "groups",
        ["group_id", "workspace_id"],
        ["id", "workspace_id"],
    )


def downgrade():
    for name in (
        "fk_group_deny_group_workspace",
        "fk_group_deny_document_workspace",
        "fk_group_deny_version_document",
        "fk_group_deny_unit_version",
    ):
        op.drop_constraint(name, "disclosure_group_denies", type_="foreignkey")
    op.create_foreign_key(
        "disclosure_group_denies_unit_id_fkey",
        "disclosure_group_denies",
        "disclosure_units",
        ["unit_id"],
        ["id"],
    )
    op.create_foreign_key(
        "disclosure_group_denies_group_id_fkey",
        "disclosure_group_denies",
        "groups",
        ["group_id"],
        ["id"],
    )
    op.drop_constraint("uq_disclosure_unit_version", "disclosure_units", type_="unique")
    for column in ("workspace_id", "document_id", "version_id"):
        op.drop_column("disclosure_group_denies", column)
