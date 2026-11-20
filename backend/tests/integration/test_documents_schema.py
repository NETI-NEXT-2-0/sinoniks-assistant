"""Document invariants on a disposable real PostgreSQL database."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from partner_agent.infra.db import get_engine, session_factory
from partner_agent.infra.models import User
from partner_agent.infra.models_documents import Document, DocumentVersion
from partner_agent.infra.settings import get_settings

pytestmark = pytest.mark.integration


async def test_active_version_cannot_reference_another_document(monkeypatch):
    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    user_id = uuid4()
    document_ids = [uuid4(), uuid4()]
    version_id = uuid4()
    try:
        async with session_factory()() as db:
            assert (await db.scalar(text("select current_database()"))).endswith("_test")
            db.add(
                User(
                    id=user_id,
                    normalized_login="document-" + user_id.hex,
                    password_hash="unused",
                    role="curator",
                    disabled=False,
                )
            )
            await db.flush()
            for identifier in document_ids:
                db.add(Document(id=identifier, owner_id=user_id, scope="knowledge", title="Курс"))
            await db.flush()
            db.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_ids[0],
                    generation=1,
                    source_sha256="a" * 64,
                    media_type="text/plain",
                    size_bytes=10,
                    original_key=f"{document_ids[0]}/{version_id}/original",
                    pipeline_version="canonical-v1",
                )
            )
            await db.commit()
        async with session_factory()() as db:
            other = await db.get(Document, document_ids[1])
            other.active_version_id = version_id
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()
        async with session_factory()() as db:
            other = await db.get(Document, document_ids[1])
            other.last_published_version_id = version_id
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()
        async with session_factory()() as db:
            original = await db.get(Document, document_ids[0])
            original.active_version_id = version_id
            await db.commit()
            assert (
                await db.scalar(
                    select(Document.active_version_id).where(Document.id == document_ids[0])
                )
                == version_id
            )
    finally:
        async with session_factory()() as db:
            for identifier in document_ids:
                doc = await db.get(Document, identifier)
                if doc:
                    doc.active_version_id = None
                    doc.last_published_version_id = None
            await db.flush()
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids))
            )
            await db.execute(delete(Document).where(Document.id.in_(document_ids)))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


@pytest.mark.parametrize("owner_role", ["curator", "admin"])
async def test_publication_requires_prepared_units_and_owner_curator(monkeypatch, owner_role):
    from partner_agent.documents.service import create_document, publish_version, unpublish
    from partner_agent.infra.errors import APIError
    from partner_agent.infra.models import AuditEvent
    from partner_agent.infra.models_documents import DisclosureUnit
    from partner_agent.retrieval.service import search_knowledge

    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    owner_id, admin_id, version_id = uuid4(), uuid4(), uuid4()
    document_id = None
    try:
        async with session_factory()() as db:
            assert (await db.scalar(text("select current_database()"))).endswith("_test")
            owner = User(
                id=owner_id,
                normalized_login="curator-" + owner_id.hex,
                password_hash="unused",
                role=owner_role,
                disabled=False,
            )
            admin = User(
                id=admin_id,
                normalized_login="admin-" + admin_id.hex,
                password_hash="unused",
                role="admin",
                disabled=False,
            )
            db.add_all([owner, admin])
            await db.flush()
            document = await create_document(db, owner, "Программа обучения", "knowledge")
            document_id = document.id
            version = DocumentVersion(
                id=version_id,
                document_id=document_id,
                generation=1,
                source_sha256="a" * 64,
                media_type="text/plain",
                size_bytes=10,
                original_key=f"{document_id}/original",
                pipeline_version="canonical-v1",
            )
            db.add(version)
            await db.commit()
        async with session_factory()() as db:
            owner = await db.get(User, owner_id)
            admin = await db.get(User, admin_id)
            with pytest.raises(APIError) as denied:
                await publish_version(db, admin, document_id, version_id, 1)
            assert denied.value.status == 404
            with pytest.raises(APIError) as pending:
                await publish_version(db, owner, document_id, version_id, 1)
            assert pending.value.code == "publication_not_ready"
            version = await db.get(DocumentVersion, version_id)
            version.status = "ready_for_review"
            version.canonical_key = f"{document_id}/{version_id}/canonical"
            version.canonical_sha256 = "c" * 64
            version.index_manifest = {
                "state": "verified",
                "version_id": str(version_id),
                "policy_revision": 1,
                "unit_count": 1,
            }
            db.add(
                DisclosureUnit(
                    version_id=version_id,
                    block_id=uuid4(),
                    text="Курс Python",
                    text_sha256="b" * 64,
                    locator={"line": 1},
                    policy_revision=1,
                )
            )
            await db.flush()
            # Legacy active pointers without explicit approval must not return evidence.
            legacy_document = await db.get(Document, document_id)
            legacy_document.active_version_id = version_id
            await db.flush()
            assert await search_knowledge(db, owner, "Python") == []
            legacy_document.active_version_id = None
            await db.flush()
            with pytest.raises(APIError) as unapproved:
                await publish_version(db, owner, document_id, version_id, 1)
            assert unapproved.value.code == "publication_not_ready"
            version.approved_canonical_sha256 = version.canonical_sha256
            document = await publish_version(db, owner, document_id, version_id, 1)
            assert document.active_version_id == version_id and document.revision == 2
            await db.rollback()
        async with session_factory()() as db:
            document = await db.get(Document, document_id)
            assert document.active_version_id is None and document.revision == 1
            # Recreate preparation after the deliberately rolled-back publication.
            version = await db.get(DocumentVersion, version_id)
            version.status = "ready_for_review"
            version.canonical_key = "canonical"
            version.canonical_sha256 = "c" * 64
            version.approved_canonical_sha256 = version.canonical_sha256
            version.index_manifest = {
                "state": "verified",
                "version_id": str(version_id),
                "policy_revision": 1,
                "unit_count": 1,
            }
            db.add(
                DisclosureUnit(
                    version_id=version_id,
                    block_id=uuid4(),
                    text="Курс Python",
                    text_sha256="b" * 64,
                    locator={"line": 1},
                    policy_revision=1,
                )
            )
            await db.flush()
            owner = await db.get(User, owner_id)
            await publish_version(db, owner, document_id, version_id, 1)
            await db.commit()
            with pytest.raises(APIError) as conflict:
                await unpublish(db, owner, document_id, 1)
            assert conflict.value.code == "revision_conflict"
            await unpublish(db, owner, document_id, 2)
            await db.commit()
            assert document.active_version_id is None and document.revision == 3
    finally:
        async with session_factory()() as db:
            if document_id:
                document = await db.get(Document, document_id)
                if document:
                    document.active_version_id = None
                    document.last_published_version_id = None
                    await db.flush()
                await db.execute(
                    delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
                )
                await db.execute(
                    delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
                )
                await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(
                delete(AuditEvent).where(AuditEvent.actor_id.in_([owner_id, admin_id]))
            )
            await db.execute(delete(User).where(User.id.in_([owner_id, admin_id])))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


async def test_text_version_admission_and_preparation_preserve_draft(monkeypatch):
    from partner_agent.documents.ingest import admit_version
    from partner_agent.documents.service import create_document
    from partner_agent.infra.models_documents import DisclosureUnit

    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    identifier = uuid4()
    doc_id = None
    try:
        async with session_factory()() as db:
            assert (await db.scalar(text("select current_database()"))).endswith("_test")
            actor = User(
                id=identifier,
                normalized_login="ingest-" + identifier.hex,
                password_hash="unused",
                role="curator",
                disabled=False,
            )
            db.add(actor)
            await db.flush()
            document = await create_document(db, actor, "Курс Python", "knowledge")
            doc_id = document.id
            source = "Урок 1\nРабота с данными".encode()
            version, created = await admit_version(db, actor, doc_id, 1, source, "text/plain")
            assert created
            await db.commit()
            units = (
                await db.scalars(
                    select(DisclosureUnit).where(DisclosureUnit.version_id == version.id)
                )
            ).all()
            assert not units
            assert document.active_version_id is None
            assert version.status == "pending" and version.index_manifest is None
    finally:
        async with session_factory()() as db:
            if doc_id:
                ids = select(DocumentVersion.id).where(DocumentVersion.document_id == doc_id)
                await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id.in_(ids)))
                await db.execute(
                    delete(DocumentVersion).where(DocumentVersion.document_id == doc_id)
                )
                await db.execute(delete(Document).where(Document.id == doc_id))
            await db.execute(delete(User).where(User.id == identifier))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


async def test_document_authorization_refreshes_disabled_owner_after_io(monkeypatch):
    from sqlalchemy import update

    from partner_agent.documents.service import create_document, lock_document
    from partner_agent.infra.errors import APIError

    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    identifier = uuid4()
    document_id = None
    try:
        async with session_factory()() as db:
            assert (await db.scalar(text("select current_database()"))).endswith("_test")
            actor = User(
                id=identifier,
                normalized_login="refresh-" + identifier.hex,
                password_hash="unused",
                role="curator",
                disabled=False,
            )
            db.add(actor)
            await db.flush()
            document = await create_document(db, actor, "Материалы", "knowledge")
            document_id = document.id
            await db.commit()
            async with session_factory()() as other:
                await other.execute(update(User).where(User.id == identifier).values(disabled=True))
                await other.commit()
            assert actor.disabled is False
            with pytest.raises(APIError) as rejected:
                await lock_document(db, document_id, actor)
            assert rejected.value.code == "access_revoked"
    finally:
        async with session_factory()() as db:
            if document_id:
                await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(delete(User).where(User.id == identifier))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


@pytest.mark.storage
async def test_explicit_docling_disclosure_omits_unapproved_content(monkeypatch):
    from hashlib import sha256

    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.disclosure import approve_stored_canonical
    from partner_agent.documents.docling_adapter import normalize_docling
    from partner_agent.documents.ingest import storage
    from partner_agent.documents.service import create_document
    from partner_agent.infra.errors import APIError
    from partner_agent.infra.models import AuditEvent
    from partner_agent.infra.models_documents import DisclosureUnit

    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    owner_id, version_id = uuid4(), uuid4()
    document_id = None
    try:
        async with session_factory()() as db:
            assert (await db.scalar(text("select current_database()"))).endswith("_test")
            owner = User(
                id=owner_id,
                normalized_login="approve-" + owner_id.hex,
                password_hash="unused",
                role="curator",
                disabled=False,
            )
            db.add(owner)
            await db.flush()
            document = await create_document(db, owner, "Материалы курса", "knowledge")
            document_id = document.id
            db.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    generation=1,
                    source_sha256="a" * 64,
                    media_type="application/pdf",
                    size_bytes=10,
                    original_key=f"{document_id}/{version_id}/original",
                    canonical_key=f"{version_id}/canonical",
                    pipeline_version="docling-adapter-v1",
                )
            )
            await db.flush()
            canonical = normalize_docling(
                {
                    "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
                    "texts": [
                        {"self_ref": "#/texts/0", "text": "Программа Python"},
                        {"self_ref": "#/texts/1", "text": "Закрытые сведения"},
                    ],
                },
                document_id,
                version_id,
                "a" * 64,
                "docling-2.150.0",
            )
            await db.commit()

            def store_canonical(value):
                adapter = storage()
                try:
                    adapter.put(
                        adapter.derived_bucket,
                        f"{version_id}/canonical",
                        value.model_dump_json().encode(),
                        "application/json",
                    )
                finally:
                    adapter.client.close()

            wrong = canonical.model_copy(update={"document_id": uuid4()})
            await run_in_threadpool(store_canonical, wrong)
            digest = sha256(wrong.model_dump_json().encode()).hexdigest()
            version = await db.get(DocumentVersion, version_id)
            version.canonical_sha256 = digest
            await db.commit()
            with pytest.raises(APIError) as mismatch:
                await approve_stored_canonical(
                    db, owner, document_id, version_id,
                    frozenset([canonical.blocks[0].id]), 1, digest
                )
            assert mismatch.value.code == "canonical_provenance_mismatch"
            await run_in_threadpool(store_canonical, canonical)
            digest = sha256(canonical.model_dump_json().encode()).hexdigest()
            version = await db.get(DocumentVersion, version_id)
            version.canonical_sha256 = digest
            await db.commit()
            units = await approve_stored_canonical(
                db, owner, document_id, version_id, frozenset([canonical.blocks[0].id]), 1, digest
            )
            assert len(units) == 1 and units[0].text == "Программа Python"
            await db.commit()
            stored = (
                await db.scalars(
                    select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
                )
            ).all()
            assert len(stored) == 1 and "Закрытые" not in stored[0].text
            assert document.active_version_id is None
    finally:

        def remove_canonical():
            adapter = storage()
            try:
                adapter.delete(adapter.derived_bucket, f"{version_id}/canonical")
            finally:
                adapter.client.close()

        await run_in_threadpool(remove_canonical)
        async with session_factory()() as db:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            if document_id:
                await db.execute(
                    delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
                )
                await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(delete(AuditEvent).where(AuditEvent.actor_id == owner_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


async def test_approval_rejects_artifact_pointer_changed_during_storage_read(monkeypatch):
    from hashlib import sha256
    from types import SimpleNamespace

    import partner_agent.documents.ingest as ingest
    from partner_agent.documents.disclosure import approve_stored_canonical
    from partner_agent.documents.docling_adapter import normalize_docling
    from partner_agent.documents.service import create_document
    from partner_agent.infra.errors import APIError
    from partner_agent.infra.models_documents import DisclosureUnit

    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    owner_id, version_id = uuid4(), uuid4()
    document_id = None
    try:
        async with session_factory()() as db:
            assert (await db.scalar(text("select current_database()"))).endswith("_test")
            owner = User(
                id=owner_id,
                normalized_login="artifact-race-" + owner_id.hex,
                password_hash="unused",
                role="curator",
                disabled=False,
            )
            db.add(owner)
            await db.flush()
            document = await create_document(db, owner, "Курс", "knowledge")
            document_id = document.id
            db.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    generation=1,
                    source_sha256="a" * 64,
                    media_type="application/pdf",
                    size_bytes=10,
                    original_key=f"{version_id}/original",
                    canonical_key="old-canonical",
                    pipeline_version="docling-adapter-v1",
                )
            )
            await db.commit()
            canonical = normalize_docling(
                {
                    "body": {"children": [{"$ref": "#/texts/0"}]},
                    "texts": [{"self_ref": "#/texts/0", "text": "Python"}],
                },
                document_id,
                version_id,
                "a" * 64,
                "docling-2.150.0",
            )

            digest = sha256(canonical.model_dump_json().encode()).hexdigest()
            version = await db.get(DocumentVersion, version_id)
            version.canonical_sha256 = digest
            await db.commit()

            def get(*args):
                # Separate real PG connection changes the pointer during the storage window.
                import psycopg

                with psycopg.connect(
                    url.replace("postgresql+psycopg://", "postgresql://", 1)
                ) as conn:
                    conn.execute(
                        "UPDATE document_versions SET canonical_key = %s WHERE id = %s",
                        ("new-canonical", version_id),
                    )
                return SimpleNamespace(data=canonical.model_dump_json().encode())

            monkeypatch.setattr(
                ingest,
                "storage",
                lambda: SimpleNamespace(
                    get=get, derived_bucket="derived", client=SimpleNamespace(close=lambda: None)
                ),
            )
            with pytest.raises(APIError) as changed:
                await approve_stored_canonical(
                    db, owner, document_id, version_id,
                    frozenset([canonical.blocks[0].id]), 1, digest
                )
            assert changed.value.code == "canonical_artifact_changed"
            await db.rollback()
            assert not (
                await db.scalars(
                    select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
                )
            ).all()
    finally:
        async with session_factory()() as db:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            if document_id:
                await db.execute(
                    delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
                )
                await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(("field", "value"), [("disabled", True), ("role", "user")])
async def test_document_lock_refreshes_owner_after_waiting_for_lock(monkeypatch, field, value):
    import asyncio

    from sqlalchemy import func, update

    from partner_agent.documents.service import lock_document
    from partner_agent.infra.errors import APIError

    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    owner_id, document_id = uuid4(), uuid4()
    try:
        async with session_factory()() as setup:
            setup.add(User(
                id=owner_id, normalized_login="lock-wait-" + owner_id.hex,
                password_hash="unused", role="curator", disabled=False,
            ))
            await setup.flush()
            setup.add(Document(id=document_id, owner_id=owner_id, scope="knowledge", title="Курс"))
            await setup.commit()
        async with session_factory()() as holder, session_factory()() as waiter:
            await holder.scalar(
                select(Document).where(Document.id == document_id).with_for_update()
            )
            actor = await waiter.get(User, owner_id)
            backend_pid = await waiter.scalar(select(func.pg_backend_pid()))
            task = asyncio.create_task(lock_document(waiter, document_id, actor))
            try:
                async with session_factory()() as observer:
                    async with asyncio.timeout(5):
                        while True:
                            waiting = await observer.scalar(
                                text("select wait_event_type = 'Lock' from pg_stat_activity "
                                     "where pid = :pid"), {"pid": backend_pid}
                            )
                            if waiting:
                                break
                            await asyncio.sleep(0.01)
                        await observer.execute(
                            update(User).where(User.id == owner_id).values(**{field: value})
                        )
                        await observer.commit()
                await holder.commit()
                if field == "disabled":
                    with pytest.raises(APIError) as rejected:
                        await task
                    assert rejected.value.code == "access_revoked"
                else:
                    await task
                    assert actor.role == "user"
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    finally:
        async with session_factory()() as db:
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


async def test_index_finalization_rejects_owner_role_revoked_during_remote_io(monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import update

    import partner_agent.documents.indexing as indexing
    from partner_agent.infra.errors import APIError
    from partner_agent.infra.models_documents import DisclosureUnit

    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    owner_id, document_id, version_id = uuid4(), uuid4(), uuid4()
    try:
        async with session_factory()() as db:
            owner = User(
                id=owner_id, normalized_login="index-role-" + owner_id.hex,
                password_hash="unused", role="curator", disabled=False,
            )
            db.add(owner)
            await db.flush()
            db.add(Document(id=document_id, owner_id=owner_id, scope="knowledge", title="Курс"))
            await db.flush()
            db.add(DocumentVersion(
                id=version_id, document_id=document_id, generation=1, source_sha256="a" * 64,
                media_type="text/plain", size_bytes=5, original_key=f"{version_id}/original",
                canonical_key=f"{version_id}/canonical", canonical_sha256="b" * 64,
                approved_canonical_sha256="b" * 64, pipeline_version="canonical-v1",
                status="processing",
            ))
            await db.flush()
            db.add(DisclosureUnit(
                version_id=version_id, block_id=uuid4(), text="Курс",
                text_sha256="c" * 64, locator={"line": 1}, policy_revision=1,
            ))
            await db.commit()

            async def revoke_during_stage(points):
                assert len(points) == 1
                async with session_factory()() as other:
                    await other.execute(update(User).where(User.id == owner_id).values(role="user"))
                    await other.commit()
                return "d" * 64

            async def already_verified(*args):
                pass

            monkeypatch.setattr(indexing, "verify_units", already_verified)
            monkeypatch.setattr(indexing, "FixtureIndex", lambda _: SimpleNamespace(
                stage=revoke_during_stage
            ))
            with pytest.raises(APIError) as rejected:
                await indexing.stage_version(db, owner, document_id, version_id)
            assert rejected.value.code == "curator_required"
            await db.rollback()
            version = await db.get(DocumentVersion, version_id, populate_existing=True)
            assert version.status == "processing" and version.index_manifest is None
    finally:
        async with session_factory()() as db:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


async def test_acl_history_blocks_accidental_physical_deletion(monkeypatch):
    from partner_agent.infra.models_documents import DisclosureAnswerDeny, DisclosureUnit

    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    owner_id, document_id, version_id, unit_id = (uuid4() for _ in range(4))
    try:
        async with session_factory()() as db:
            assert (await db.scalar(text("select current_database()"))).endswith("_test")
            db.add(User(id=owner_id, normalized_login="acl-history-" + owner_id.hex,
                        password_hash="unused", role="curator", disabled=False))
            await db.flush()
            db.add(Document(id=document_id, owner_id=owner_id, scope="knowledge", title="Курс"))
            await db.flush()
            db.add(DocumentVersion(id=version_id, document_id=document_id, generation=1,
                                   source_sha256="a" * 64, media_type="text/plain", size_bytes=1,
                                   original_key=f"{version_id}/original",
                                   pipeline_version="text-v1"))
            await db.flush()
            db.add(DisclosureUnit(id=unit_id, version_id=version_id, block_id=uuid4(),
                                  text="Защищённый текст", text_sha256="b" * 64,
                                  locator={"line": 1}, policy_revision=1))
            document = await db.get(Document, document_id)
            document.last_published_version_id = version_id
            await db.flush()
            db.add(DisclosureAnswerDeny(unit_id=unit_id, user_id=owner_id))
            await db.commit()
        async with session_factory()() as db:
            with pytest.raises(IntegrityError) as unit_blocked:
                await db.execute(delete(DisclosureUnit).where(DisclosureUnit.id == unit_id))
            assert unit_blocked.value.orig.diag.constraint_name == (
                "disclosure_answer_denies_unit_id_fkey"
            )
            await db.rollback()
            # Remove dependent rows only within the transaction to isolate the version FK.
            await db.execute(
                delete(DisclosureAnswerDeny).where(DisclosureAnswerDeny.unit_id == unit_id)
            )
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.id == unit_id))
            with pytest.raises(IntegrityError) as version_blocked:
                await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            assert version_blocked.value.orig.diag.constraint_name == (
                "fk_document_last_published_version"
            )
            await db.rollback()
            assert await db.get(DisclosureAnswerDeny, (unit_id, owner_id)) is not None
            assert (await db.get(Document, document_id)).last_published_version_id == version_id
    finally:
        async with session_factory()() as db:
            await db.execute(
                delete(DisclosureAnswerDeny).where(DisclosureAnswerDeny.unit_id == unit_id)
            )
            document = await db.get(Document, document_id)
            if document:
                document.last_published_version_id = None
                await db.flush()
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.id == unit_id))
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


async def test_audience_lock_serializes_member_changes_with_final_read(monkeypatch):
    import asyncio

    from sqlalchemy import func

    from partner_agent.auth.audience_lock import audience_read_lock, audience_write_lock

    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL database required")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        async with session_factory()() as reader, session_factory()() as writer:
            assert (await reader.scalar(text("select current_database()"))).endswith("_test")
            await audience_read_lock(reader)
            pid = await writer.scalar(select(func.pg_backend_pid()))
            task = asyncio.create_task(audience_write_lock(writer))
            try:
                async with session_factory()() as observer:
                    async with asyncio.timeout(5):
                        while not await observer.scalar(
                            text("select wait_event_type = 'Lock' from pg_stat_activity "
                                 "where pid = :pid"),
                            {"pid": pid},
                        ):
                            await asyncio.sleep(0.01)
                assert not task.done()
                await reader.commit()
                await asyncio.wait_for(task, timeout=5)
                await writer.commit()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    finally:
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
