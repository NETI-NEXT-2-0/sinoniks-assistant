"""Stage immutable draft units, verify Qdrant, then advance readiness in PostgreSQL."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.documents.service import lock_document
from partner_agent.documents.verify_disclosure import verify_units
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_documents import DisclosureUnit, DocumentVersion
from partner_agent.infra.settings import get_settings
from partner_agent.retrieval.index import COLLECTION, FixtureIndex


def unit_snapshot(units: list[DisclosureUnit]) -> str:
    payload = [
        {
            "id": str(unit.id),
            "block_id": str(unit.block_id),
            "text": unit.text,
            "text_sha256": unit.text_sha256,
            "locator": unit.locator,
            "policy_revision": unit.policy_revision,
        }
        for unit in units
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


async def stage_version(db: AsyncSession, actor: User, document_id: UUID, version_id: UUID) -> None:
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    version = await db.get(DocumentVersion, version_id)
    if version is None or version.document_id != document.id:
        raise APIError(404, "not_found", "Version not found")
    if (
        version.status != "processing"
        or not version.canonical_key
        or not version.canonical_sha256
        or version.approved_canonical_sha256 != version.canonical_sha256
        or (
            version.pipeline_version == "docling-adapter-v1"
            and version.disclosure_format not in ("docling-context-v2", "curator-edited-v1")
        )
    ):
        raise APIError(409, "version_state_conflict", "Version not ready for indexing")
    artifact_hash = version.canonical_sha256
    reviewed_copy_sha256, disclosure_format = (
        version.reviewed_copy_sha256,
        version.disclosure_format,
    )
    canonical_key, source_hash, media_type, pipeline = (
        version.canonical_key,
        version.source_sha256,
        version.media_type,
        version.pipeline_version,
    )
    revision = document.policy_revision
    units = list(
        (
            await db.scalars(
                select(DisclosureUnit)
                .where(
                    DisclosureUnit.version_id == version_id,
                    DisclosureUnit.policy_revision == revision,
                )
                .order_by(DisclosureUnit.id)
            )
        ).all()
    )
    units_hash = unit_snapshot(units)
    points = [
        {
            "id": str(unit.id),
            "vector": {},
            "payload": {
                "document_id": str(document.id),
                "version_id": str(version_id),
                "unit_id": str(unit.id),
                "block_id": str(unit.block_id),
                "text": unit.text,
                "text_sha256": unit.text_sha256,
                "policy_revision": revision,
                "owner_id": str(document.owner_id),
                "profile": "offline_lexical_fixture",
            },
        }
        for unit in units
    ]
    await db.commit()
    await verify_units(
        document_id,
        version_id,
        canonical_key,
        artifact_hash,
        source_hash,
        media_type,
        pipeline,
        revision,
        units,
        reviewed_copy_sha256,
    )
    digest = await FixtureIndex(get_settings().qdrant_url).stage(points)
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    version = await db.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.id == version_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        version is None
        or version.status != "processing"
        or document.policy_revision != revision
        or version.canonical_sha256 != artifact_hash
        or version.canonical_key != canonical_key
        or version.source_sha256 != source_hash
        or version.media_type != media_type
        or version.pipeline_version != pipeline
        or version.reviewed_copy_sha256 != reviewed_copy_sha256
        or version.disclosure_format != disclosure_format
        or version.approved_canonical_sha256 != artifact_hash
        or (
            version.pipeline_version == "docling-adapter-v1"
            and version.disclosure_format not in ("docling-context-v2", "curator-edited-v1")
        )
    ):
        raise APIError(409, "index_state_changed", "Version changed during indexing")
    current_units = list(
        (
            await db.scalars(
                select(DisclosureUnit)
                .where(
                    DisclosureUnit.version_id == version_id,
                    DisclosureUnit.policy_revision == revision,
                )
                .order_by(DisclosureUnit.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if unit_snapshot(current_units) != units_hash:
        raise APIError(409, "index_state_changed", "Disclosure changed during indexing")
    version.index_manifest = {
        "state": "verified",
        "version_id": str(version_id),
        "policy_revision": revision,
        "unit_count": len(units),
        "collection": COLLECTION,
        "payload_sha256": digest,
        "profile": "offline_lexical_fixture",
    }
    version.index_attempts = 0
    version.index_retry_at = None
    version.status = "ready_for_review"
    await db.flush()
