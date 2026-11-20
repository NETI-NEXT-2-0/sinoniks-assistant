"""Optional supplied-corpus acceptance, only disposable accounts/DB and real storage."""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from test_chat_data_plane import plane as plane
from test_demo_retrieval import cleanup

from partner_agent.documents.indexing import stage_version
from partner_agent.documents.ingest import process_stored_text
from partner_agent.infra.db import session_factory
from partner_agent.infra.models import User
from partner_agent.knowledge19 import QUESTIONS, load_checks, load_sources
from partner_agent.runtime.rag_provider import citation_location

pytestmark = [pytest.mark.integration, pytest.mark.storage]
KNOWLEDGE = "/api/v1/knowledge"


async def test_supplied_knowledge19_upload_publish_grant_revoke(plane):
    if os.environ.get("PARTNER_KNOWLEDGE19_TEST_CONFIRMED") != "1":
        pytest.skip("Explicit disposable supplied-corpus confirmation required")
    source_path = os.environ.get("PARTNER_KNOWLEDGE19_SOURCE")
    if not source_path:
        pytest.fail("Supply the private corpus directory explicitly")
    sources = load_sources(Path(source_path))
    async with session_factory()() as db:
        assert (await db.scalar(text("select current_database()"))).endswith("_test")
    owner, owner_headers, owner_id = await plane(role="curator")
    reader, reader_headers, reader_id = await plane()
    outsider, outsider_headers, _ = await plane(role="admin")
    documents, versions = {}, {}
    source_texts = {source.name: source.raw.decode("utf-8") for source in sources}

    async def search(client, headers, query):
        response = await client.post(f"{KNOWLEDGE}/search", headers=headers, json={"query": query})
        assert response.status_code == 200
        assert response.json()["profile"] == "lexical_bm25_ru_v1"
        return response.json()["items"]

    try:
        for source in sources:
            created = await owner.post(
                f"{KNOWLEDGE}/documents",
                headers=owner_headers,
                json={"title": "Синоникс 1.9 · " + source.name},
            )
            assert created.status_code == 201
            document_id = documents[source.name] = UUID(created.json()["id"])
            uploaded = await owner.post(
                f"{KNOWLEDGE}/documents/{document_id}/versions",
                params={"expected_revision": 1},
                content=source.raw,
                headers={
                    **owner_headers,
                    "Content-Type": "text/markdown",
                    "Idempotency-Key": str(uuid4()),
                },
            )
            assert uploaded.status_code == 202
            version_id = versions[source.name] = UUID(uploaded.json()["id"])
            async with session_factory()() as db:
                actor = await db.get(User, owner_id)
                await process_stored_text(db, actor, document_id, version_id)
                await db.commit()
            path = f"{KNOWLEDGE}/versions/{version_id}"
            review = await owner.get(f"{path}/review")
            assert review.status_code == 200
            assert not await search(reader, reader_headers, "Синоникс")
            approved = await owner.post(
                f"{path}/approve",
                headers=owner_headers,
                json={
                    "expected_revision": 2,
                    "canonical_sha256": review.json()["canonical_sha256"],
                    "reviewed_text": source_texts[source.name],
                },
            )
            assert approved.status_code == 202
            async with session_factory()() as db:
                actor = await db.get(User, owner_id)
                await stage_version(db, actor, document_id, version_id)
                await db.commit()
            # Approval/indexing alone is not publication.
            assert all(
                hit["document_id"] != str(document_id)
                for hit in await search(owner, owner_headers, "Синоникс")
            )
            published = await owner.post(
                f"{path}/publish", headers=owner_headers, json={"expected_revision": 2}
            )
            assert published.status_code == 200
            assert not await search(reader, reader_headers, "Синоникс")
            assert not await search(outsider, outsider_headers, "Синоникс")
            downloaded = await owner.post(f"{path}/original/download", headers=owner_headers)
            assert downloaded.status_code == 200
            assert hashlib.sha256(downloaded.content).hexdigest() == source.digest

        for document_id in documents.values():
            granted = await owner.put(
                f"{KNOWLEDGE}/documents/{document_id}/grants/{reader_id}",
                headers=owner_headers,
                json={"expected_revision": 3, "allow_answer": True, "allow_original": False},
            )
            assert granted.status_code == 200

        async def evaluate(checks):
            outcomes = []
            for identifier, query, expected_file, section in checks:
                hits = await search(reader, reader_headers, query)
                for hit in hits:
                    source_name = next(
                        name
                        for name, value in documents.items()
                        if str(value) == hit["document_id"]
                    )
                    locator = hit["locator"]
                    assert locator["reviewed_copy"] is True
                    assert (
                        source_texts[source_name][locator["start"] : locator["end"]]
                        == hit["excerpt"]
                    )
                    if locator.get("section"):
                        assert citation_location(locator).startswith("Раздел проверенной редакции:")
                ranks = [
                    n + 1
                    for n, hit in enumerate(hits)
                    if hit["document_id"] == str(documents[expected_file])
                    and hit["locator"].get("section", "").startswith(section)
                ]
                outcomes.append(
                    {"id": identifier, "expected_section_rank": min(ranks) if ranks else None}
                )
            return outcomes

        original = await evaluate(QUESTIONS)
        holdout = await evaluate(
            load_checks(Path(__file__).resolve().parents[3] / "docs/knowledge19-holdout.json")
        )
        assert sum(row["expected_section_rank"] is not None for row in original) == 11
        assert all(row["expected_section_rank"] is not None for row in holdout)
        for version_id in versions.values():
            path = f"{KNOWLEDGE}/versions/{version_id}"
            assert (
                await reader.post(f"{path}/original/download", headers=reader_headers)
            ).status_code == 404
            assert (await outsider.get(f"{path}/review")).status_code == 404
        revoked = sources[0].name
        response = await owner.put(
            f"{KNOWLEDGE}/documents/{documents[revoked]}/grants/{reader_id}",
            headers=owner_headers,
            json={"expected_revision": 4, "allow_answer": False, "allow_original": False},
        )
        assert response.status_code == 200
        assert all(
            hit["document_id"] != str(documents[revoked])
            for hit in await search(reader, reader_headers, "антивирус ICAP правила")
        )
        assert any(
            hit["document_id"] == str(documents[revoked])
            for hit in await search(owner, owner_headers, "антивирус ICAP правила")
        )
        if report_path := os.environ.get("PARTNER_KNOWLEDGE19_TEST_REPORT"):
            report = {
                "schema_version": 1,
                "profile": "real_pg_garage_qdrant_supplied_corpus",
                "source_sha256": {source.name: source.digest for source in sources},
                "original_checks": original,
                "additional_checks": holdout,
                "original_top8": 11,
                "additional_top8": 12,
                "upload_review_approve_index_publish_verified": True,
                "source_bytes_verified": True,
                "excerpt_offsets_verified": True,
                "grant_revoke_admin_isolation_verified": True,
                "answer_permission_does_not_allow_original": True,
                "model_answers_verified": False,
                "model_calls": 0,
            }
            with Path(report_path).open("x", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
    finally:
        await cleanup(list(documents.values()))
