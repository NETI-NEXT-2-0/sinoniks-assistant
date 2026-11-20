"""Admin-managed workspace and group directory; membership grants no content access."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update

from partner_agent.auth.admin import Admin
from partner_agent.auth.audience_lock import audience_write_lock
from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.auth.routes import DB, api_rate_limit
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import AuditEvent, Group, GroupMember, Membership, User, Workspace

router = APIRouter(
    prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(api_rate_limit)]
)


class NameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)


def clean_name(value: str) -> str:
    name = value.strip()
    if not name or any(ord(c) < 32 for c in name):
        raise APIError(422, "invalid_name", "Invalid name")
    return name


async def require_current_admin(db: DB, actor: User) -> None:
    await db.refresh(actor)
    if actor.disabled or actor.role not in MANAGER_ROLES:
        raise APIError(403, "admin_required", "Administrator access required")


async def workspace_locked(db: DB, workspace_id: UUID, actor: User) -> Workspace:
    workspace = await db.scalar(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    await require_current_admin(db, actor)
    if workspace is None:
        raise APIError(404, "not_found", "Workspace not found")
    return workspace


def audit(db: DB, actor: User, action: str, resource: str, changed: bool) -> None:
    db.add(AuditEvent(actor_id=actor.id, action=action, resource_id=resource,
                      result="success" if changed else "unchanged"))


@router.post("/workspaces", status_code=201)
async def create_workspace(body: NameBody, actor: Admin, db: DB) -> dict[str, str]:
    await require_current_admin(db, actor)
    workspace = Workspace(name=clean_name(body.name))
    db.add(workspace)
    await db.flush()
    audit(db, actor, "admin.workspace.create", str(workspace.id), True)
    result = {"id": str(workspace.id), "name": workspace.name}
    await db.commit()
    return result


@router.get("/workspaces")
async def list_workspaces(
    actor: Admin, db: DB, response: Response, cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    query = select(Workspace).order_by(Workspace.id).limit(limit + 1)
    if cursor:
        query = query.where(Workspace.id > cursor)
    rows = list((await db.scalars(query)).all())
    response.headers["Cache-Control"] = "private, no-store"
    result = {"items": [{"id": str(item.id), "name": item.name} for item in rows[:limit]],
              "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None}
    await db.commit()
    return result


@router.put("/workspaces/{workspace_id}/members/{user_id}")
async def add_workspace_member(
    workspace_id: UUID, user_id: UUID, actor: Admin, db: DB,
) -> dict[str, Any]:
    await audience_write_lock(db)
    await workspace_locked(db, workspace_id, actor)
    user = await db.get(User, user_id)
    if user is None or user.disabled:
        raise APIError(422, "invalid_member", "Active user required")
    member = await db.get(Membership, (workspace_id, user_id))
    if member is None:
        db.add(Membership(workspace_id=workspace_id, user_id=user_id, role="member"))
    audit(db, actor, "admin.workspace.member.add", f"{workspace_id}:{user_id}", member is None)
    await db.commit()
    return {"workspace_id": str(workspace_id), "user_id": str(user_id), "member": True}


@router.delete("/workspaces/{workspace_id}/members/{user_id}")
async def remove_workspace_member(
    workspace_id: UUID, user_id: UUID, actor: Admin, db: DB,
) -> dict[str, Any]:
    await audience_write_lock(db)
    await workspace_locked(db, workspace_id, actor)
    member = await db.get(Membership, (workspace_id, user_id))
    groups = select(GroupMember.group_id).join(Group, Group.id == GroupMember.group_id).where(
        Group.workspace_id == workspace_id, GroupMember.user_id == user_id,
    )
    await db.execute(update(Group).where(Group.id.in_(groups)).values(
        membership_revision=Group.membership_revision + 1
    ))
    workspace_groups = select(Group.id).where(Group.workspace_id == workspace_id)
    await db.execute(delete(GroupMember).where(
        GroupMember.group_id.in_(workspace_groups), GroupMember.user_id == user_id,
    ))
    if member is not None:
        await db.delete(member)
    audit(db, actor, "admin.workspace.member.remove", f"{workspace_id}:{user_id}",
          member is not None)
    await db.commit()
    return {"workspace_id": str(workspace_id), "user_id": str(user_id), "member": False}


@router.get("/workspaces/{workspace_id}/members")
async def list_workspace_members(
    workspace_id: UUID, actor: Admin, db: DB, response: Response, cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    await workspace_locked(db, workspace_id, actor)
    query = (
        select(User).join(Membership, Membership.user_id == User.id)
        .where(Membership.workspace_id == workspace_id).order_by(User.id).limit(limit + 1)
    )
    if cursor:
        query = query.where(User.id > cursor)
    rows = list((await db.scalars(query)).all())
    response.headers["Cache-Control"] = "private, no-store"
    result = {
        "items": [{"id": str(user.id), "login": user.normalized_login,
                   "disabled": user.disabled} for user in rows[:limit]],
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }
    await db.commit()
    return result


@router.post("/workspaces/{workspace_id}/groups", status_code=201)
async def create_group(
    workspace_id: UUID, body: NameBody, actor: Admin, db: DB,
) -> dict[str, Any]:
    workspace = await workspace_locked(db, workspace_id, actor)
    group = Group(workspace_id=workspace.id, name=clean_name(body.name))
    db.add(group)
    await db.flush()
    audit(db, actor, "admin.group.create", str(group.id), True)
    result = {"id": str(group.id), "workspace_id": str(workspace.id), "name": group.name}
    await db.commit()
    return result


@router.get("/workspaces/{workspace_id}/groups")
async def list_groups(
    workspace_id: UUID, actor: Admin, db: DB, response: Response, cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    await workspace_locked(db, workspace_id, actor)
    query = (
        select(Group).where(Group.workspace_id == workspace_id).order_by(Group.id).limit(limit + 1)
    )
    if cursor:
        query = query.where(Group.id > cursor)
    rows = list((await db.scalars(query)).all())
    response.headers["Cache-Control"] = "private, no-store"
    result = {"items": [{"id": str(item.id), "workspace_id": str(workspace_id),
                         "name": item.name} for item in rows[:limit]],
              "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None}
    await db.commit()
    return result


@router.put("/workspaces/{workspace_id}/groups/{group_id}/members/{user_id}")
@router.delete("/workspaces/{workspace_id}/groups/{group_id}/members/{user_id}")
async def set_group_member(
    workspace_id: UUID, group_id: UUID, user_id: UUID, actor: Admin, db: DB,
    request: Request,
) -> dict[str, Any]:
    await audience_write_lock(db)
    await workspace_locked(db, workspace_id, actor)
    group = await db.get(Group, group_id)
    if group is None or group.workspace_id != workspace_id:
        raise APIError(404, "not_found", "Group not found")
    current = await db.get(GroupMember, (group_id, user_id))
    adding = request.method == "PUT"
    if adding:
        user = await db.get(User, user_id)
        member = await db.get(Membership, (workspace_id, user_id))
        if user is None or user.disabled or member is None:
            raise APIError(422, "invalid_member", "Active workspace member required")
        if current is None:
            db.add(GroupMember(group_id=group_id, user_id=user_id, workspace_id=workspace_id))
    elif current is not None:
        await db.delete(current)
    changed = (adding and current is None) or (not adding and current is not None)
    if changed:
        group.membership_revision += 1
    audit(db, actor, "admin.group.member.add" if adding else "admin.group.member.remove",
          f"{group_id}:{user_id}", changed)
    await db.commit()
    return {"group_id": str(group_id), "user_id": str(user_id), "member": adding}


@router.get("/workspaces/{workspace_id}/groups/{group_id}/members")
async def list_group_members(
    workspace_id: UUID, group_id: UUID, actor: Admin, db: DB, response: Response,
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    await workspace_locked(db, workspace_id, actor)
    group = await db.get(Group, group_id)
    if group is None or group.workspace_id != workspace_id:
        raise APIError(404, "not_found", "Group not found")
    query = (
        select(User).join(GroupMember, GroupMember.user_id == User.id)
        .where(GroupMember.group_id == group_id).order_by(User.id).limit(limit + 1)
    )
    if cursor:
        query = query.where(User.id > cursor)
    rows = list((await db.scalars(query)).all())
    response.headers["Cache-Control"] = "private, no-store"
    result = {
        "items": [{"id": str(user.id), "login": user.normalized_login,
                   "disabled": user.disabled} for user in rows[:limit]],
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }
    await db.commit()
    return result
