"""Curator-facing public search, intentionally separate from chat execution."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.auth.routes import DB, api_rate_limit, authenticated, increment, unsafe_session
from partner_agent.auth.security import digest
from partner_agent.infra.errors import APIError
from partner_agent.infra.settings import get_settings
from partner_agent.web.search import (
    MAX_QUERY_CHARS,
    official_domains,
    search_official,
    search_parameters,
)

router = APIRouter(prefix="/api/v1/web", tags=["web"], dependencies=[Depends(api_rate_limit)])


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str = Field(min_length=1, max_length=253)
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    confirmed_public: Literal[True]


@router.get("/official-domains")
async def domains(request: Request, response: Response, db: DB) -> dict[str, tuple[str, ...]]:
    _, actor = await authenticated(request, db)
    if actor.role not in MANAGER_ROLES:
        raise APIError(403, "curator_required", "Curator permission required")
    await db.commit()
    response.headers["Cache-Control"] = "private, no-store"
    return {"domains": official_domains(get_settings())}


@router.post("/search")
async def search(body: SearchBody, request: Request, response: Response, db: DB) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    if actor.role not in MANAGER_ROLES:
        raise APIError(403, "curator_required", "Curator permission required")
    settings = get_settings()
    parameters = search_parameters(settings, body.domain, body.query)
    count = await increment(db, "web-search:" + digest(str(actor.id)))
    await db.commit()
    if count > 12:
        raise APIError(429, "web_search_rate_limited", "Too many web searches", True)
    items, partial = await search_official(settings.searxng_url, body.domain, parameters)
    response.headers["Cache-Control"] = "private, no-store"
    return {"items": items, "partial": partial}
