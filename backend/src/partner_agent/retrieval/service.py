"""Grant-aware offline lexical search; current PostgreSQL state is authoritative."""

from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from partner_agent.documents.access import (
    access_predicate,
    group_access_allowed,
    group_unit_denied,
    lock_authorized_document,
    unit_answer_allowed,
    unit_answer_predicate,
)
from partner_agent.documents.limits import MAX_EVIDENCE_CHARS
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_documents import (
    DisclosureAnswerDeny,
    DisclosureUnit,
    Document,
    DocumentGrant,
    DocumentVersion,
)
from partner_agent.retrieval.lexical import matched_terms, query_terms

SCAN_PAGE_SIZE = 1000
MAX_SCAN_CANDIDATES = 10000


def searchable_evidence_predicate(actor: User) -> ColumnElement[bool]:
    """Filters for the active-version join shared by retrieval and corpus counts."""
    return and_(
        access_predicate(actor, "answer"),
        unit_answer_predicate(actor),
        Document.scope == "knowledge",
        Document.deleted_at.is_(None),
        DisclosureUnit.policy_revision == Document.policy_revision,
        func.char_length(DisclosureUnit.text) <= MAX_EVIDENCE_CHARS,
        DocumentVersion.status == "ready_for_review",
        DocumentVersion.canonical_sha256.is_not(None),
        DocumentVersion.approved_canonical_sha256 == DocumentVersion.canonical_sha256,
        or_(
            DocumentVersion.pipeline_version != "docling-adapter-v1",
            DocumentVersion.disclosure_format.in_(("docling-context-v2", "curator-edited-v1")),
        ),
    )


async def knowledge_corpus_summary(db: AsyncSession, actor: User) -> dict[str, int]:
    totals = (
        select(
            func.count(Document.id).label("documents"),
            func.count(Document.active_version_id).label("published_documents"),
        )
        .where(
            Document.owner_id == actor.id,
            Document.scope == "knowledge",
            Document.deleted_at.is_(None),
        )
        .subquery()
    )
    searchable = (
        select(
            func.count(Document.id.distinct()).label("searchable_documents"),
            func.count(DocumentVersion.source_url.distinct()).label("unique_source_urls"),
            func.count(DisclosureUnit.id).label("searchable_excerpts"),
        )
        .select_from(DisclosureUnit)
        .join(DocumentVersion, DisclosureUnit.version_id == DocumentVersion.id)
        .join(Document, Document.active_version_id == DocumentVersion.id)
        .where(Document.owner_id == actor.id, searchable_evidence_predicate(actor))
        .subquery()
    )
    query = select(totals, searchable).select_from(totals.join(searchable, true()))
    counts = await db.execute(query)
    return dict(counts.mappings().one())


async def validate_evidence_access(
    db: AsyncSession, actor: User, evidence: list[dict[str, Any]], *, chat_id: UUID | None = None
) -> None:
    from partner_agent.documents.chat_files import validate_file_evidence

    private = [item for item in evidence if item.get("source_kind") == "attachment"]
    if private:
        if chat_id is None:
            raise APIError(409, "source_context_changed", "Missing chat scope")
        await validate_file_evidence(db, actor, chat_id, private)
    evidence = [item for item in evidence if item.get("source_kind") != "attachment"]
    documents = {}
    for identifier in sorted({UUID(item["document_id"]) for item in evidence}):
        try:
            documents[identifier] = await lock_authorized_document(db, actor, identifier, "answer")
        except APIError as error:
            if error.status == 404:
                raise APIError(409, "source_context_changed", "Source context changed") from None
            raise
    for item in evidence:
        document = documents[UUID(item["document_id"])]
        if str(document.active_version_id) != item["version_id"]:
            raise APIError(409, "source_context_changed", "Source context changed")
        unit = await db.scalar(
            select(DisclosureUnit)
            .join(DocumentVersion, DocumentVersion.id == DisclosureUnit.version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DisclosureUnit.id == UUID(item["evidence_id"]),
                DisclosureUnit.version_id == UUID(item["version_id"]),
                Document.id == document.id,
                DisclosureUnit.policy_revision == Document.policy_revision,
                unit_answer_predicate(actor),
                DocumentVersion.status == "ready_for_review",
                DocumentVersion.canonical_sha256.is_not(None),
                DocumentVersion.approved_canonical_sha256 == DocumentVersion.canonical_sha256,
                or_(
                    DocumentVersion.pipeline_version != "docling-adapter-v1",
                    DocumentVersion.disclosure_format.in_(
                        ("docling-context-v2", "curator-edited-v1")
                    ),
                ),
            )
            .execution_options(populate_existing=True)
        )
        if unit is None or unit.text != item["excerpt"]:
            raise APIError(409, "source_context_changed", "Source context changed")


async def search_knowledge(
    db: AsyncSession, actor: User, query: str, limit: int = 8
) -> list[dict[str, Any]]:
    current = await db.scalar(
        select(User).where(User.id == actor.id).execution_options(populate_existing=True)
    )
    if current is None or current.disabled:
        raise APIError(403, "access_revoked", "Access revoked")
    if not query.strip() or len(query.encode()) > 32768 or not 1 <= limit <= 100:
        raise APIError(422, "validation_error", "Invalid query")
    terms, identifiers = query_terms(query)
    if not terms:
        return []
    # Access filters run before loading any content. Keyset pages avoid silently
    # omitting matching IDs after the first page. The explicit cap bounds work.
    query_rows = (
        select(DisclosureUnit, Document)
        .join(DocumentVersion, DisclosureUnit.version_id == DocumentVersion.id)
        .join(Document, Document.active_version_id == DocumentVersion.id)
        .where(searchable_evidence_predicate(current))
        .order_by(DisclosureUnit.id)
    )
    hits: list[dict[str, Any]] = []
    cursor: UUID | None = None
    scanned = 0
    while True:
        page_query = query_rows
        if cursor is not None:
            page_query = page_query.where(DisclosureUnit.id > cursor)
        page_size = min(SCAN_PAGE_SIZE, MAX_SCAN_CANDIDATES - scanned)
        rows = (await db.execute(page_query.limit(page_size + 1))).all()
        page = rows[:page_size]
        for unit, document in page:
            matches = matched_terms(terms, identifiers, unit.text)
            if not matches:
                continue
            hits.append(
                {
                    "evidence_id": str(unit.id),
                    "document_id": str(document.id),
                    "version_id": str(unit.version_id),
                    "title": document.title,
                    "excerpt": unit.text,
                    "locator": unit.locator,
                    "score": len(matches) / len(terms),
                    "profile": "offline_lexical_only",
                    "_policy_revision": unit.policy_revision,
                }
            )
        scanned += len(page)
        if len(rows) <= page_size:
            break
        if scanned >= MAX_SCAN_CANDIDATES:
            raise APIError(503, "search_scope_too_large", "Search scope exceeds the current limit")
        cursor = page[-1][0].id
    selected = sorted(hits, key=lambda hit: (-hit["score"], hit["evidence_id"]))[:limit]
    # Recheck under document locks after candidate loading; revocations and pointer
    # changes serialize with this final check. Lock in a stable order across searches.
    authorized = {}
    for identifier in sorted({UUID(hit["document_id"]) for hit in selected}):
        try:
            authorized[identifier] = await lock_authorized_document(
                db, current, identifier, "answer"
            )
        except APIError as error:
            if error.status != 404:
                raise
    result = []
    for hit in selected:
        authorized_doc = authorized.get(UUID(hit["document_id"]))
        if authorized_doc is None or (
            str(authorized_doc.active_version_id) != hit["version_id"]
            or authorized_doc.policy_revision != hit["_policy_revision"]
        ):
            continue
        denied = await db.get(
            DisclosureAnswerDeny, (UUID(hit["evidence_id"]), current.id), populate_existing=True
        )
        current_grant = await db.get(
            DocumentGrant, (authorized_doc.id, current.id), populate_existing=True
        )
        group_allowed = await group_access_allowed(db, current, authorized_doc, "answer")
        denied_by_group = await group_unit_denied(
            db, current, authorized_doc, UUID(hit["evidence_id"])
        )
        if not unit_answer_allowed(
            current,
            authorized_doc,
            current_grant,
            denied is not None or denied_by_group,
            group_allowed,
        ):
            continue
        version = await db.get(DocumentVersion, UUID(hit["version_id"]), populate_existing=True)
        if version is None or (
            version.status != "ready_for_review"
            or not version.canonical_sha256
            or version.approved_canonical_sha256 != version.canonical_sha256
            or (
                version.pipeline_version == "docling-adapter-v1"
                and version.disclosure_format not in ("docling-context-v2", "curator-edited-v1")
            )
        ):
            continue
        del hit["_policy_revision"]
        web_source = version.web_source()
        if web_source is not None:
            hit["web_source"] = web_source
        result.append(hit)
    return result


async def search_answer_evidence(
    db: AsyncSession,
    actor: User,
    question: str,
    history: list[dict[str, str]],
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Prefer the current question; use history only as a bounded empty-search fallback."""
    evidence = await search_knowledge(db, actor, question, limit=limit)
    _, identifiers = query_terms(question)
    # Explicit identifiers belong to this question. An unknown new code must not
    # silently retrieve an older code from conversation history instead.
    if evidence or identifiers:
        return evidence
    previous = next((item["content"] for item in reversed(history) if item["role"] == "user"), "")
    if not previous or previous == question:
        return evidence
    expanded = f"{previous}\n{question}"
    if len(expanded.encode()) > 32768:
        return evidence
    return await search_knowledge(db, actor, expanded, limit=limit)
