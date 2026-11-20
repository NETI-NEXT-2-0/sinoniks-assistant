"""Bounded document audience directory; PostgreSQL remains authoritative."""

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from partner_agent.documents.access import document_access_allowed
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import Group, GroupMember, User
from partner_agent.infra.models_documents import (
    DisclosureAnswerDeny,
    DisclosureGroupDeny,
    DisclosureUnit,
    Document,
    DocumentGrant,
    DocumentGroupGrant,
    DocumentVersion,
)

MAX_RELATED_GROUPS = 1000


async def audience_page(
    db: AsyncSession, actor: User, document: Document, after: UUID | None,
    limit: int, expected_snapshot: str | None,
) -> dict[str, Any]:
    """Caller holds the audience read lock, document lock and workspace authorization."""
    linked_grant = exists(select(DocumentGroupGrant.group_id).where(
        DocumentGroupGrant.document_id == document.id, DocumentGroupGrant.group_id == Group.id,
    ).correlate(Group))
    linked_deny = exists(select(DisclosureGroupDeny.group_id).where(
        DisclosureGroupDeny.document_id == document.id, DisclosureGroupDeny.group_id == Group.id,
    ).correlate(Group))
    groups = (await db.execute(
        select(Group.id, Group.membership_revision)
        .where(Group.workspace_id == document.workspace_id, or_(linked_grant, linked_deny))
        .order_by(Group.id).limit(MAX_RELATED_GROUPS + 1)
    )).all() if document.workspace_id is not None else []
    if len(groups) > MAX_RELATED_GROUPS:
        raise APIError(409, "audience_scope_too_large", "Too many related groups to review")
    snapshot = hashlib.sha256(json.dumps({
        "actor": str(actor.id), "document": str(document.id),
        "workspace": str(document.workspace_id), "acl_revision": document.acl_revision,
        "groups": [(str(group_id), revision) for group_id, revision in groups],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if after is not None and expected_snapshot is None:
        raise APIError(422, "snapshot_required", "Restart audience review")
    if expected_snapshot is not None and expected_snapshot != snapshot:
        raise APIError(409, "revision_conflict", "Audience changed")

    direct_deny = exists(
        select(DisclosureAnswerDeny.user_id)
        .join(DisclosureUnit, DisclosureUnit.id == DisclosureAnswerDeny.unit_id)
        .join(DocumentVersion, DocumentVersion.id == DisclosureUnit.version_id)
        .where(DocumentVersion.document_id == document.id, DisclosureAnswerDeny.user_id == User.id)
        .correlate(User)
    )
    group_base = (
        select(DocumentGroupGrant.group_id)
        .join(GroupMember, and_(GroupMember.group_id == DocumentGroupGrant.group_id,
                                GroupMember.workspace_id == DocumentGroupGrant.workspace_id))
        .where(DocumentGroupGrant.document_id == document.id,
               DocumentGroupGrant.workspace_id == document.workspace_id,
               GroupMember.user_id == User.id)
        .correlate(User)
    )
    group_answer = exists(group_base.where(DocumentGroupGrant.allow_answer.is_(True)))
    group_original = exists(group_base.where(DocumentGroupGrant.allow_original.is_(True)))
    group_deny = exists(
        select(DisclosureGroupDeny.group_id)
        .join(GroupMember, and_(GroupMember.group_id == DisclosureGroupDeny.group_id,
                                GroupMember.workspace_id == DisclosureGroupDeny.workspace_id))
        .where(DisclosureGroupDeny.document_id == document.id,
               DisclosureGroupDeny.workspace_id == document.workspace_id,
               GroupMember.user_id == User.id)
        .correlate(User)
    )
    query = (
        select(User, DocumentGrant, group_answer, group_original, direct_deny, group_deny)
        .outerjoin(DocumentGrant, and_(DocumentGrant.document_id == document.id,
                                       DocumentGrant.user_id == User.id))
        .where(or_(User.id == document.owner_id, DocumentGrant.user_id.is_not(None),
                   group_answer, group_original, direct_deny, group_deny))
    )
    if after is not None:
        query = query.where(User.id > after)
    rows = (await db.execute(query.order_by(User.id).limit(limit + 1))).all()
    items = []
    for target, grant, by_group_answer, by_group_original, personal_deny, by_group_deny in (
        rows[:limit]
    ):
        items.append({
            "user_id": str(target.id), "login": target.normalized_login,
            "disabled": target.disabled, "owner": target.id == document.owner_id,
            "direct_answer": bool(grant and grant.allow_answer),
            "direct_original": bool(grant and grant.allow_original),
            "group_answer": bool(by_group_answer), "group_original": bool(by_group_original),
            "policy_allows_answer": document_access_allowed(
                target, document, grant, "answer", bool(by_group_answer)
            ),
            "policy_allows_original": document_access_allowed(
                target, document, grant, "original", bool(by_group_original)
            ),
            "has_denies": bool(personal_deny or by_group_deny),
        })
    return {
        "items": items, "snapshot_token": snapshot,
        "next_after": str(rows[limit - 1][0].id) if len(rows) > limit else None,
    }
