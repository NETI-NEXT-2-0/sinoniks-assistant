"""Real PG/Garage/Qdrant: edited copy is authoritative, original remains private."""

import hashlib
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from test_chat_data_plane import plane as plane

from partner_agent.documents.canonical import extract_utf8
from partner_agent.documents.docling_adapter import normalize_docling
from partner_agent.documents.edited_copy import copy_key
from partner_agent.documents.indexing import stage_version
from partner_agent.documents.ingest import storage
from partner_agent.infra.db import session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion
from partner_agent.retrieval.index import FixtureIndex
from partner_agent.retrieval.service import search_knowledge

pytestmark = [pytest.mark.integration, pytest.mark.storage]
SOURCE = b"Course fee 100. Contact alice@example.test. Terms remain."
EDIT = "Course fee 120. Contact . Terms remain.\nAdded public note."


@pytest.mark.parametrize("binary,quality", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("tamper", [False, True])
async def test_reviewed_copy_is_exact_and_bound_to_approved_artifact(
    plane, binary, quality, tamper,
):
    client, headers, owner = await plane(role="admin")
    other, other_headers, _ = await plane(role="admin")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Reviewed course"}
    )
    document_id, version_id = UUID(created.json()["id"]), uuid4()
    source_hash = hashlib.sha256(SOURCE).hexdigest()
    canonical = (
        normalize_docling(
            {
                "body": {"children": [{"$ref": "#/texts/0"}]},
                "texts": [{"self_ref": "#/texts/0", "text": SOURCE.decode()}],
            },
            document_id,
            version_id,
            source_hash,
            "docling-2.150.0",
        )
        if binary
        else extract_utf8(SOURCE, document_id, version_id, "text/plain")
    )
    key, original_key = f"{document_id}/{version_id}/canonical.json", f"{version_id}/original"
    if quality:
        canonical = canonical.model_copy(update={"warnings": ("native_text_order_uncertain",)})
    adapter = storage()
    digest = adapter.put(
        adapter.derived_bucket, key, canonical.model_dump_json().encode(), "application/json"
    )
    adapter.put(adapter.originals_bucket, original_key, SOURCE, "application/octet-stream")
    edited_key = None
    try:
        async with session_factory()() as db:
            db.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    generation=1,
                    source_sha256=source_hash,
                    media_type="application/pdf" if binary else "text/plain",
                    size_bytes=len(SOURCE),
                    original_key=original_key,
                    canonical_key=key,
                    canonical_sha256=digest,
                    pipeline_version=canonical.pipeline_version,
                    status="pending",
                )
            )
            await db.commit()
        body = {"expected_revision": 1, "canonical_sha256": digest, "reviewed_text": EDIT}
        path = f"/api/v1/knowledge/versions/{version_id}/approve"
        assert (await other.post(path, headers=other_headers, json=body)).status_code == 404
        assert (
            await client.post(path, headers=headers, json={
                **body, "reviewed_text": " ", "text_order_confirmed": quality,
            })
        ).status_code == 422
        assert (
            await client.post(path, headers=headers, json={**body, "canonical_sha256": "f" * 64})
        ).status_code == 409
        if quality:
            blocked = await client.post(path, headers=headers, json=body)
            assert blocked.status_code == 409
            assert blocked.json()["code"] == "canonical_review_required"
            body["text_order_confirmed"] = True
        response = await client.post(path, headers=headers, json=body)
        assert response.status_code == 202, response.text
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            assert version.disclosure_format == "curator-edited-v1"
            assert version.canonical_sha256 == digest
            edited_key = copy_key(document_id, version_id, version.reviewed_copy_sha256)
            saved_copy = adapter.get(adapter.derived_bucket, edited_key).data
            assert json.loads(saved_copy).get("text_order_confirmed", False) is quality
            units = list(
                (
                    await db.scalars(
                        select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
                    )
                ).all()
            )
            assert (
                "".join(unit.text for unit in sorted(units, key=lambda u: u.locator["start"]))
                == EDIT
            )
            assert all(
                unit.locator["reviewed_copy"] and "positions" not in unit.locator for unit in units
            )
        assert adapter.get(adapter.originals_bucket, original_key).data == SOURCE
        await FixtureIndex("http://qdrant:6333").ensure_compatible()
        if tamper:
            adapter.put(adapter.derived_bucket, edited_key, b"{}", "application/json")
            async with session_factory()() as db:
                actor = await db.get(User, owner)
                with pytest.raises(APIError, match="Reviewed copy changed"):
                    await stage_version(db, actor, document_id, version_id)
            return
        async with session_factory()() as db:
            actor = await db.get(User, owner)
            await stage_version(db, actor, document_id, version_id)
            await db.commit()
        result = await client.post(
            f"/api/v1/knowledge/versions/{version_id}/publish",
            headers=headers,
            json={"expected_revision": 1},
        )
        assert result.status_code == 200, result.text
        async with session_factory()() as db:
            actor = await db.get(User, owner)
            assert not await search_knowledge(db, actor, "alice@example.test")
            assert not await search_knowledge(db, actor, "100")
            hits = await search_knowledge(db, actor, "120")
            assert hits and hits[0]["excerpt"] == EDIT and hits[0]["locator"]["reviewed_copy"]
    finally:
        async with session_factory()() as db:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            document = await db.get(Document, document_id)
            document.active_version_id = None
            document.last_published_version_id = None
            await db.flush()
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()
        adapter.delete(adapter.derived_bucket, key)
        adapter.delete(adapter.originals_bucket, original_key)
        if edited_key:
            adapter.delete(adapter.derived_bucket, edited_key)
        adapter.client.close()
