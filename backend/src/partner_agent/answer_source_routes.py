"""Reopen a cited fragment under current permissions, independently of original access."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, Response
from sqlalchemy import select

from partner_agent.auth.routes import DB, api_rate_limit, authenticated
from partner_agent.documents.access import document_access_allowed, group_access_allowed
from partner_agent.documents.chat_files import restore_file_evidence
from partner_agent.infra.errors import APIError
from partner_agent.infra.models_documents import (
    DisclosureUnit,
    Document,
    DocumentGrant,
    DocumentVersion,
)
from partner_agent.infra.models_stage2 import ContextManifest, Run
from partner_agent.retrieval.service import validate_evidence_access
from partner_agent.runs.service import lock_chat
from partner_agent.runtime.rag_provider import citation_location

router = APIRouter(prefix="/api/v1/runs", tags=["answer sources"],
                   dependencies=[Depends(api_rate_limit)])


@router.get("/{run_id}/citations/{index}")
async def read_citation(
    run_id: UUID, index: Annotated[int, Path(ge=0, le=49)], request: Request,
    response: Response, db: DB,
) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    chat_id = await db.scalar(select(Run.chat_id).where(Run.id == run_id))
    if chat_id is None:
        raise APIError(404, "not_found", "Source not found")
    await lock_chat(db, chat_id, actor)
    run = await db.get(Run, run_id)
    citations = (run.outcome or {}).get("citations") if run else None
    if (not run or run.status != "succeeded" or not isinstance(citations, list)
            or index >= len(citations)):
        raise APIError(404, "not_found", "Source not found")
    citation = citations[index]
    identifier = citation.get("evidence_id") if isinstance(citation, dict) else None
    quote = citation.get("quote") if isinstance(citation, dict) else None
    manifest = await db.scalar(select(ContextManifest).where(ContextManifest.run_id == run_id))
    if (not isinstance(identifier, str) or not isinstance(quote, str) or not quote.strip()
            or len(quote) > 1600 or manifest is None or identifier not in manifest.evidence_ids):
        raise APIError(404, "not_found", "Source not found")
    download_path = None
    web_source = None
    if identifier.startswith("attachment:"):
        evidence = await restore_file_evidence(db, actor, chat_id, identifier)
    else:
        try:
            unit_id = UUID(identifier)
        except ValueError:
            raise APIError(404, "not_found", "Source not found") from None
        unit = await db.get(DisclosureUnit, unit_id)
        version = await db.get(DocumentVersion, unit.version_id) if unit else None
        if unit is None or version is None:
            raise APIError(409, "source_context_changed", "Source changed")
        evidence = {"evidence_id": identifier, "document_id": str(version.document_id),
                    "version_id": str(version.id), "excerpt": unit.text, "locator": unit.locator}
        await validate_evidence_access(db, actor, [evidence], chat_id=chat_id)
        document = await db.get(Document, version.document_id, populate_existing=True)
        assert document is not None
        evidence["title"] = document.title
        grant = await db.get(DocumentGrant, (document.id, actor.id), populate_existing=True)
        original_group = await group_access_allowed(db, actor, document, "original")
        if document_access_allowed(actor, document, grant, "original", original_group):
            download_path = f"/knowledge/versions/{version.id}/original/download"
        web_source = version.web_source()
    if (len(evidence["excerpt"]) > 1600
            or " ".join(quote.split()) not in " ".join(evidence["excerpt"].split())):
        raise APIError(409, "source_context_changed", "Quoted text changed")
    title = evidence.get("title", "Документ")
    if evidence["locator"].get("reviewed_copy") is True:
        title = "Проверенная редакция · " + title
    result = {"evidence_id": identifier, "title": title, "quote": quote,
              "excerpt": evidence["excerpt"], "location": citation_location(evidence["locator"]),
              "original_download_path": download_path, "web_source": web_source}
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result
