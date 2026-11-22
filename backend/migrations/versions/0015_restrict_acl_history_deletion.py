"""Block accidental deletion of publication and unit restriction history."""

from alembic import op

revision = "0015_acl_history_guard"
down_revision = "0014_publication_acl"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("fk_document_last_published_version", "documents", type_="foreignkey")
    op.create_foreign_key(
        "fk_document_last_published_version", "documents", "document_versions",
        ["last_published_version_id", "id"], ["id", "document_id"],
    )
    op.drop_constraint(
        "disclosure_answer_denies_unit_id_fkey", "disclosure_answer_denies", type_="foreignkey"
    )
    op.create_foreign_key(
        "disclosure_answer_denies_unit_id_fkey", "disclosure_answer_denies", "disclosure_units",
        ["unit_id"], ["id"],
    )


def downgrade():
    op.drop_constraint(
        "disclosure_answer_denies_unit_id_fkey", "disclosure_answer_denies", type_="foreignkey"
    )
    op.create_foreign_key(
        "disclosure_answer_denies_unit_id_fkey", "disclosure_answer_denies", "disclosure_units",
        ["unit_id"], ["id"], ondelete="CASCADE",
    )
    op.drop_constraint("fk_document_last_published_version", "documents", type_="foreignkey")
    op.create_foreign_key(
        "fk_document_last_published_version", "documents", "document_versions",
        ["last_published_version_id", "id"], ["id", "document_id"],
        ondelete="SET NULL (last_published_version_id)",
    )
