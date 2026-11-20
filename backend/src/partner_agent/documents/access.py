"""Central predicates for owner and explicit direct knowledge permissions."""

from typing import Literal
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from partner_agent.auth.audience_lock import audience_read_lock
from partner_agent.auth.policy import MANAGER_ROLES, Principal, Resource, authorize
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import Group, GroupMember, Membership, User
from partner_agent.infra.models_documents import (
    DisclosureAnswerDeny,
    DisclosureGroupDeny,
    DisclosureUnit,
    Document,
    DocumentGrant,
    DocumentGroupGrant,
)

KnowledgeAction = Literal["answer", "original"]
DocumentAction = Literal["answer", "original", "manage", "acl"]


def access_predicate(actor: User, action: KnowledgeAction) -> ColumnElement[bool]:
    """A grant covers exactly one action; curator ownership is an independent basis."""
    if action not in ("answer", "original"):
        raise ValueError("unknown_knowledge_action")
    permission = DocumentGrant.allow_answer if action == "answer" else DocumentGrant.allow_original
    grant = exists(
        select(DocumentGrant.document_id).where(
            DocumentGrant.document_id == Document.id,
            DocumentGrant.user_id == actor.id,
            permission.is_(True),
        ).correlate(Document)
    )
    group_permission = (
        DocumentGroupGrant.allow_answer if action == "answer" else DocumentGroupGrant.allow_original
    )
    group_grant = exists(
        select(DocumentGroupGrant.document_id)
        .join(Group, Group.id == DocumentGroupGrant.group_id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .join(Membership, and_(Membership.workspace_id == Group.workspace_id,
                               Membership.user_id == GroupMember.user_id))
        .where(DocumentGroupGrant.document_id == Document.id,
               DocumentGroupGrant.workspace_id == Document.workspace_id,
               Group.workspace_id == Document.workspace_id,
               GroupMember.user_id == actor.id, group_permission.is_(True))
        .correlate(Document)
    )
    active = exists(select(User.id).where(User.id == actor.id, User.disabled.is_(False)))
    owner = and_(
        Document.owner_id == actor.id,
        exists(select(User.id).where(User.id == actor.id, User.role.in_(MANAGER_ROLES))),
    )
    return and_(active, or_(owner, grant, group_grant))


def document_access_allowed(
    actor: User, document: Document, grant: DocumentGrant | None, action: DocumentAction,
    group_allowed: bool = False,
) -> bool:
    allowed: set[str] = set()
    if document.owner_id == actor.id and actor.role in MANAGER_ROLES:
        allowed.update((
            "knowledge.answer", "knowledge.excerpt", "original.download",
            "document.manage", "acl.manage",
        ))
    if action not in ("answer", "original", "manage", "acl"):
        return False
    if grant is not None and grant.user_id == actor.id and grant.document_id == document.id:
        if grant.allow_answer:
            allowed.update(("knowledge.answer", "knowledge.excerpt"))
        if grant.allow_original:
            allowed.add("original.download")
    if group_allowed and action == "answer":
        allowed.update(("knowledge.answer", "knowledge.excerpt"))
    if group_allowed and action == "original":
        allowed.add("original.download")
    policy_action = {
        "answer": "knowledge.answer", "original": "original.download",
        "manage": "document.manage", "acl": "acl.manage",
    }[action]
    return authorize(
        Principal(actor.id, actor.role, actor.disabled), policy_action,
        Resource(owner_id=document.owner_id, allowed_actions=frozenset(allowed)),
    )


def unit_answer_predicate(actor: User) -> ColumnElement[bool]:
    direct = exists(
        select(DisclosureAnswerDeny.unit_id).where(
            DisclosureAnswerDeny.unit_id == DisclosureUnit.id,
            DisclosureAnswerDeny.user_id == actor.id,
        ).correlate(DisclosureUnit)
    )
    group = exists(
        select(DisclosureGroupDeny.unit_id)
        .join(Group, Group.id == DisclosureGroupDeny.group_id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .join(Membership, and_(Membership.workspace_id == Group.workspace_id,
                               Membership.user_id == GroupMember.user_id))
        .where(DisclosureGroupDeny.unit_id == DisclosureUnit.id,
               Group.workspace_id == Document.workspace_id, GroupMember.user_id == actor.id)
        .correlate(DisclosureUnit, Document)
    )
    return and_(~direct, ~group)


def unit_answer_allowed(
    actor: User, document: Document, grant: DocumentGrant | None, denied: bool,
    group_allowed: bool = False,
) -> bool:
    if not document_access_allowed(actor, document, grant, "answer", group_allowed):
        return False
    return authorize(
        Principal(actor.id, actor.role, actor.disabled), "knowledge.answer",
        Resource(allowed_actions=frozenset({"knowledge.answer"}),
                 denied_actions=frozenset({"knowledge.answer"}) if denied else frozenset()),
    )


async def lock_authorized_document(
    db: AsyncSession, actor: User, document_id: UUID, action: KnowledgeAction
) -> Document:
    # Lock before checking grants: a revoke that held this lock must be visible
    # to the subsequent READ COMMITTED statement, even if we waited for it.
    await audience_read_lock(db)
    document = await db.scalar(
        select(Document)
        .where(Document.id == document_id, Document.scope == "knowledge",
               Document.deleted_at.is_(None))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current = await db.scalar(
        select(User).where(User.id == actor.id).execution_options(populate_existing=True)
    )
    if current is None or current.disabled:
        raise APIError(403, "access_revoked", "Access revoked")
    if document is None:
        raise APIError(404, "not_found", "Document not found")
    grant = await db.get(DocumentGrant, (document_id, current.id), populate_existing=True)
    group_allowed = await group_access_allowed(db, current, document, action)
    if not document_access_allowed(current, document, grant, action, group_allowed):
        raise APIError(404, "not_found", "Document not found")
    return document


async def group_access_allowed(
    db: AsyncSession, actor: User, document: Document, action: KnowledgeAction,
) -> bool:
    """Caller holds the shared audience lock before relying on this result."""
    if document.workspace_id is None:
        return False
    permission = (
        DocumentGroupGrant.allow_answer if action == "answer" else DocumentGroupGrant.allow_original
    )
    query = (select(DocumentGroupGrant.document_id)
        .join(Group, Group.id == DocumentGroupGrant.group_id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .join(Membership, and_(Membership.workspace_id == Group.workspace_id,
                               Membership.user_id == GroupMember.user_id))
        .where(DocumentGroupGrant.document_id == document.id,
               DocumentGroupGrant.workspace_id == document.workspace_id,
               Group.workspace_id == document.workspace_id,
               GroupMember.user_id == actor.id, permission.is_(True)))
    return bool(await db.scalar(select(exists(query))))


async def group_unit_denied(
    db: AsyncSession, actor: User, document: Document, unit_id: UUID,
) -> bool:
    """Caller holds the audience lock while checking group membership."""
    if document.workspace_id is None:
        return False
    query = (select(DisclosureGroupDeny.unit_id)
        .join(Group, Group.id == DisclosureGroupDeny.group_id)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .join(Membership, and_(Membership.workspace_id == Group.workspace_id,
                               Membership.user_id == GroupMember.user_id))
        .where(DisclosureGroupDeny.unit_id == unit_id,
               Group.workspace_id == document.workspace_id, GroupMember.user_id == actor.id))
    return bool(await db.scalar(select(exists(query))))
