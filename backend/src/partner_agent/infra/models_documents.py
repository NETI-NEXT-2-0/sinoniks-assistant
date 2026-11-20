"""Document ownership, immutable source versions and approved disclosure content."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from partner_agent.infra.models import Base, utcnow


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="uq_document_workspace"),
        CheckConstraint("scope IN ('private', 'knowledge')", name="ck_document_scope"),
        CheckConstraint("revision >= 1 AND policy_revision >= 1", name="ck_document_revision"),
        CheckConstraint("acl_revision >= 1", name="ck_document_acl_revision"),
        ForeignKeyConstraint(
            ["active_version_id", "id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_document_active_version",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["last_published_version_id", "id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_document_last_published_version",
            use_alter=True,
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", name="fk_documents_workspace")
    )
    scope: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    policy_revision: Mapped[int] = mapped_column(Integer, default=1)
    acl_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    active_version_id: Mapped[uuid.UUID | None] = mapped_column()
    last_published_version_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("id", "document_id", name="uq_version_document"),
        UniqueConstraint("document_id", "generation", name="uq_document_generation"),
        UniqueConstraint("document_id", "upload_idempotency_key", name="uq_document_upload_key"),
        CheckConstraint(
            "generation >= 1 AND size_bytes > 0 AND size_bytes <= 26214400",
            name="ck_version_bounds",
        ),
        CheckConstraint("parser_attempts >= 0 AND parser_attempts <= 5", name="ck_parser_attempts"),
        CheckConstraint("index_attempts >= 0 AND index_attempts <= 5", name="ck_index_attempts"),
        CheckConstraint("text_attempts >= 0 AND text_attempts <= 5", name="ck_text_attempts"),
        CheckConstraint(
            "(source_url IS NULL) = (source_captured_on IS NULL)",
            name="ck_version_web_source_pair",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready_for_review', 'failed')",
            name="ck_version_status",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    upload_idempotency_key: Mapped[uuid.UUID | None] = mapped_column()
    source_sha256: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    source_captured_on: Mapped[date | None] = mapped_column(Date)
    media_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    original_key: Mapped[str] = mapped_column(String(200), unique=True)
    canonical_key: Mapped[str | None] = mapped_column(String(200))
    canonical_sha256: Mapped[str | None] = mapped_column(String(64))
    approved_canonical_sha256: Mapped[str | None] = mapped_column(String(64))
    disclosure_format: Mapped[str | None] = mapped_column(String(32))
    reviewed_copy_sha256: Mapped[str | None] = mapped_column(String(64))
    pipeline_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64))
    parser_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parser_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    index_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    text_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


    def web_source(self) -> dict[str, str] | None:
        if self.source_url is None or self.source_captured_on is None:
            return None
        return {"url": self.source_url, "captured_on": self.source_captured_on.isoformat()}


class DisclosureUnit(Base):
    __tablename__ = "disclosure_units"
    __table_args__ = (
        UniqueConstraint("version_id", "block_id", name="uq_disclosure_block"),
        UniqueConstraint("id", "version_id", name="uq_disclosure_unit_version"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id"), index=True)
    block_id: Mapped[uuid.UUID] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB)
    policy_revision: Mapped[int] = mapped_column(Integer)


class DocumentGrant(Base):
    __tablename__ = "document_grants"
    __table_args__ = (
        CheckConstraint("allow_answer OR allow_original", name="ck_document_grant_nonempty"),
    )
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    allow_answer: Mapped[bool] = mapped_column(Boolean)
    allow_original: Mapped[bool] = mapped_column(Boolean)


class DisclosureAnswerDeny(Base):
    __tablename__ = "disclosure_answer_denies"
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("disclosure_units.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)


class DocumentGroupGrant(Base):
    __tablename__ = "document_group_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "workspace_id"], ["documents.id", "documents.workspace_id"]
        ),
        ForeignKeyConstraint(["group_id", "workspace_id"], ["groups.id", "groups.workspace_id"]),
        CheckConstraint("allow_answer OR allow_original", name="ck_group_grant_nonempty"),
    )
    document_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column()
    allow_answer: Mapped[bool] = mapped_column(Boolean)
    allow_original: Mapped[bool] = mapped_column(Boolean)


class DisclosureGroupDeny(Base):
    __tablename__ = "disclosure_group_denies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["unit_id", "version_id"],
            ["disclosure_units.id", "disclosure_units.version_id"],
            name="fk_group_deny_unit_version",
        ),
        ForeignKeyConstraint(
            ["version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_group_deny_version_document",
        ),
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_group_deny_document_workspace",
        ),
        ForeignKeyConstraint(
            ["group_id", "workspace_id"],
            ["groups.id", "groups.workspace_id"],
            name="fk_group_deny_group_workspace",
        ),
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    version_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column()
    workspace_id: Mapped[uuid.UUID] = mapped_column()
