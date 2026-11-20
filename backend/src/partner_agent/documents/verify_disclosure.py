"""Verify persisted disclosure units against the hash-bound canonical artifact."""

import hashlib
from uuid import UUID, uuid5

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from partner_agent.documents.canonical import CanonicalDocument
from partner_agent.documents.disclosure import render_approved_docling
from partner_agent.documents.docling_adapter import ParsedDocument
from partner_agent.documents.edited_copy import ReviewedCopy, copy_key, render_copy
from partner_agent.documents.ingest import storage
from partner_agent.infra.errors import APIError
from partner_agent.infra.models_documents import DisclosureUnit


async def verify_units(
    document_id: UUID,
    version_id: UUID,
    key: str,
    artifact_hash: str,
    source_hash: str,
    media_type: str,
    pipeline: str,
    policy_revision: int,
    units: list[DisclosureUnit],
    reviewed_copy_sha256: str | None = None,
) -> None:
    def load() -> ParsedDocument | CanonicalDocument:
        adapter = storage()
        try:
            data = adapter.get(adapter.derived_bucket, key).data
            if hashlib.sha256(data).hexdigest() != artifact_hash:
                raise APIError(409, "canonical_digest_mismatch", "Canonical artifact changed")
            if pipeline == "docling-adapter-v1":
                return ParsedDocument.model_validate_json(data)
            if pipeline == "canonical-v1":
                return CanonicalDocument.model_validate_json(data)
            raise APIError(409, "canonical_artifact_invalid", "Unsupported canonical artifact")
        finally:
            adapter.client.close()

    try:
        canonical = await run_in_threadpool(load)
    except ValidationError:
        raise APIError(
            409, "canonical_artifact_invalid", "Canonical artifact requires repair"
        ) from None
    if (
        canonical.document_id != document_id
        or canonical.version_id != version_id
        or canonical.source_sha256 != source_hash
        or canonical.pipeline_version != pipeline
        or (isinstance(canonical, CanonicalDocument) and canonical.media_type != media_type)
    ):
        raise APIError(409, "canonical_provenance_mismatch", "Canonical provenance mismatch")
    if not units or len(units) > 1000:
        raise APIError(409, "disclosure_integrity_mismatch", "Disclosure requires repair")
    selected = {unit.block_id for unit in units}
    if reviewed_copy_sha256 is not None:

        def load_copy() -> ReviewedCopy:
            adapter = storage()
            try:
                data = adapter.get(
                    adapter.derived_bucket, copy_key(document_id, version_id, reviewed_copy_sha256)
                ).data
                if hashlib.sha256(data).hexdigest() != reviewed_copy_sha256:
                    raise APIError(409, "disclosure_integrity_mismatch", "Reviewed copy changed")
                return ReviewedCopy.model_validate_json(data)
            finally:
                adapter.client.close()

        try:
            copy = await run_in_threadpool(load_copy)
        except ValidationError:
            raise APIError(409, "disclosure_integrity_mismatch", "Invalid reviewed copy") from None
        if (
            copy.document_id != document_id
            or copy.version_id != version_id
            or copy.canonical_sha256 != artifact_hash
        ):
            raise APIError(409, "disclosure_integrity_mismatch", "Reviewed copy provenance changed")
        rendered = render_copy(copy, reviewed_copy_sha256)
    elif isinstance(canonical, ParsedDocument):
        try:
            for unit in units:
                context = unit.locator.get("context")
                if not isinstance(context, list):
                    raise ValueError("invalid_context")
                for item in context:
                    if not isinstance(item, dict) or not isinstance(item.get("block_id"), str):
                        raise ValueError("invalid_context")
                    selected.add(UUID(item["block_id"]))
            if len(selected) > 1000:
                raise ValueError("too_many_blocks")
            rendered = render_approved_docling(canonical, frozenset(selected))
        except (ValueError, KeyError):
            raise APIError(
                409, "disclosure_integrity_mismatch", "Disclosure requires repair"
            ) from None
    else:
        by_id = {block.id: block for block in canonical.blocks}
        if not selected <= by_id.keys():
            raise APIError(409, "disclosure_integrity_mismatch", "Disclosure requires repair")
        rendered = [
            (block.id, block.text, block.text_sha256, block.locator.model_dump())
            for block in canonical.blocks
            if block.id in selected
        ]
    expected = {item[0]: item[1:] for item in rendered}
    if len(expected) != len(units):
        raise APIError(409, "disclosure_integrity_mismatch", "Disclosure requires repair")
    for unit in units:
        expected_id = uuid5(version_id, f"disclosure:{unit.block_id}:{policy_revision}")
        if (
            unit.id != expected_id
            or unit.policy_revision != policy_revision
            or expected.get(unit.block_id) != (unit.text, unit.text_sha256, unit.locator)
        ):
            raise APIError(409, "disclosure_integrity_mismatch", "Disclosure requires repair")
