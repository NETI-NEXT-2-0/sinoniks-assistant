"""Account administration grants no access to private chat or knowledge content."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, SecretStr, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.auth.routes import (
    DB,
    api_rate_limit,
    authenticated,
    revoke_user_sessions,
    unsafe_session,
)
from partner_agent.auth.security import normalize_login, password_hasher
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import AuditEvent, User

router = APIRouter(
    prefix="/api/v1/admin/users", tags=["admin"], dependencies=[Depends(api_rate_limit)]
)
Role = Literal["user", "curator", "admin"]


class UserView(BaseModel):
    id: UUID
    login: str
    role: Role
    disabled: bool
    revision: int
    created_at: datetime


def view(user: User) -> UserView:
    return UserView(
        id=user.id,
        login=user.normalized_login,
        role=user.role,
        disabled=user.disabled,
        revision=user.revision,
        created_at=user.created_at,
    )


class UsersPage(BaseModel):
    items: list[UserView]
    next_cursor: UUID | None


class CreateUser(BaseModel):
    login: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=12, max_length=1024)
    role: Role = "user"


class UpdateUser(BaseModel):
    expected_revision: int = Field(ge=1)
    role: Role | None = None
    disabled: bool | None = None

    @model_validator(mode="after")
    def changes_required(self) -> "UpdateUser":
        if self.role is None and self.disabled is None:
            raise ValueError("At least one account change is required")
        return self


async def administrator(request: Request, db: DB) -> User:
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        await unsafe_session(request, db)
    _, user = await authenticated(request, db)
    if user.role not in MANAGER_ROLES:
        raise APIError(403, "admin_required", "Administrator access required")
    return user


Admin = Annotated[User, Depends(administrator)]


@router.get("")
async def list_users(
    actor: Admin,
    db: DB,
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> UsersPage:
    query = select(User).order_by(User.id).limit(limit + 1)
    if cursor:
        query = query.where(User.id > cursor)
    users = list((await db.scalars(query)).all())
    page = users[:limit]
    result = UsersPage(
        items=[view(user) for user in page],
        next_cursor=page[-1].id if len(users) > limit else None,
    )
    await db.commit()
    return result


@router.post("", status_code=201)
async def create_user(body: CreateUser, actor: Admin, db: DB) -> UserView:
    try:
        login = normalize_login(body.login)
    except ValueError:
        raise APIError(422, "invalid_login", "Invalid login") from None
    hashed = await run_in_threadpool(password_hasher.hash, body.password.get_secret_value())
    # Same operator/bootstrap serialization key as the local first-admin CLI.
    await db.execute(text("SELECT pg_advisory_xact_lock(8642917501)"))
    user = User(normalized_login=login, password_hash=hashed, role=body.role)
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise APIError(409, "login_exists", "Login already exists") from None
    db.add(
        AuditEvent(
            actor_id=actor.id,
            action="admin.user.create",
            resource_id=str(user.id),
            result="success",
        )
    )
    result = view(user)
    await db.commit()
    return result


@router.patch("/{user_id}")
async def update_user(user_id: UUID, body: UpdateUser, actor: Admin, db: DB) -> UserView:
    await db.execute(text("SELECT pg_advisory_xact_lock(8642917501)"))
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise APIError(404, "not_found", "Resource not found")
    if user.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "Account changed; reload before retrying")
    role = body.role if body.role is not None else user.role
    disabled = body.disabled if body.disabled is not None else user.disabled
    changed = (role, disabled) != (user.role, user.disabled)
    if user.role in MANAGER_ROLES and not user.disabled and (role not in MANAGER_ROLES or disabled):
        admins = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role.in_(MANAGER_ROLES), User.disabled.is_(False))
        )
        if (admins or 0) <= 1:
            raise APIError(409, "last_administrator", "Keep at least one active administrator")
    if changed:
        user.role, user.disabled = role, disabled
        user.revision += 1
        await revoke_user_sessions(db, user.id)
    db.add(
        AuditEvent(
            actor_id=actor.id,
            action="admin.user.update",
            resource_id=str(user.id),
            result="success" if changed else "unchanged",
        )
    )
    result = view(user)
    await db.commit()
    return result
