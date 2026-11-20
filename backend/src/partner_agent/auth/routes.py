import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from partner_agent.auth.security import (
    DUMMY_PASSWORD_HASH,
    digest,
    new_secret,
    normalize_login,
    password_hasher,
)
from partner_agent.infra.db import database_session
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import AuditEvent, RateLimit, Session, User, utcnow
from partner_agent.infra.settings import get_settings

DB = Annotated[AsyncSession, Depends(database_session)]
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginBody(BaseModel):
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def cookie(response: Response, value: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.cookie_name,
        value,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
        max_age=settings.absolute_ttl_seconds,
    )


async def increment(db: AsyncSession, key: str) -> int:
    now = utcnow()
    window = now.replace(second=0, microsecond=0)
    result = await db.execute(
        insert(RateLimit)
        .values(key=key, window_start=window, count=1)
        .on_conflict_do_update(
            index_elements=[RateLimit.key, RateLimit.window_start],
            set_={"count": RateLimit.count + 1},
        )
        .returning(RateLimit.count)
    )
    # Expired counters contain only keyed digests, but need not grow forever.
    await db.execute(delete(RateLimit).where(RateLimit.window_start < now - timedelta(minutes=2)))
    return result.scalar_one()


async def api_rate_limit(request: Request, db: DB) -> None:
    ip = request.client.host if request.client else "unknown"
    count = await increment(db, "api:" + digest(ip))
    await db.commit()
    if count > get_settings().api_requests_per_minute:
        raise APIError(429, "api_rate_limited", "Too many requests", True)


async def load_session(request: Request, db: AsyncSession) -> Session:
    token = request.cookies.get(get_settings().cookie_name, "")
    if not token or len(token) > 256:
        raise APIError(401, "session_required", "Sign in required")
    session = await db.scalar(
        select(Session).where(Session.token_hash == digest(token)).with_for_update()
    )
    now = utcnow()
    if (
        session is None
        or session.revoked_at is not None
        or session.expires_at <= now
        or session.absolute_expires_at <= now
    ):
        raise APIError(401, "session_expired", "Sign in required")
    return session


async def unsafe_session(request: Request, db: AsyncSession) -> Session:
    if request.headers.get("origin") != get_settings().public_origin:
        raise APIError(403, "origin_rejected", "Request origin rejected")
    session = await load_session(request, db)
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied or not secrets.compare_digest(digest(supplied), session.csrf_hash):
        raise APIError(403, "csrf_rejected", "Invalid CSRF token")
    return session


async def authenticated(request: Request, db: AsyncSession) -> tuple[Session, User]:
    session = await load_session(request, db)
    user = await db.get(User, session.user_id) if session.user_id else None
    if user is None or user.disabled:
        raise APIError(401, "session_required", "Sign in required")
    now = utcnow()
    session.last_seen_at = now
    session.expires_at = min(
        now + timedelta(seconds=get_settings().idle_ttl_seconds), session.absolute_expires_at
    )
    return session, user


@router.get("/csrf", dependencies=[Depends(api_rate_limit)])
async def csrf(request: Request, response: Response, db: DB) -> dict[str, str]:
    # An existing authenticated cookie may obtain a new synchronizer token after a reload.
    token = request.cookies.get(get_settings().cookie_name, "")
    existing = None
    if token:
        try:
            existing = await load_session(request, db)
        except APIError:
            pass
    csrf_token = new_secret()
    if existing is not None:
        existing.csrf_hash = digest(csrf_token)
    else:
        now = utcnow()
        token = new_secret()
        expiry = now + timedelta(seconds=get_settings().anonymous_ttl_seconds)
        db.add(
            Session(
                token_hash=digest(token),
                csrf_hash=digest(csrf_token),
                expires_at=expiry,
                absolute_expires_at=expiry,
            )
        )
    await db.commit()
    cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": csrf_token}


@router.post("/login", dependencies=[Depends(api_rate_limit)])
async def login(body: LoginBody, request: Request, response: Response, db: DB) -> dict[str, object]:
    session = await unsafe_session(request, db)
    try:
        normalized = normalize_login(body.login)
    except ValueError:
        normalized = ""  # Same response and password work as an unknown account.
    ip = request.client.host if request.client else "unknown"
    keys = ["login:" + digest(normalized), "ip:" + digest(ip)]
    # Serialize attempts for both limits; consistent order avoids deadlocks.
    from sqlalchemy import text

    for key in sorted(keys):
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
        )
    window = utcnow().replace(second=0, microsecond=0)
    for key in keys:
        count = await db.scalar(
            select(RateLimit.count).where(RateLimit.key == key, RateLimit.window_start == window)
        )
        if (count or 0) >= get_settings().login_failures_per_minute:
            db.add(AuditEvent(action="auth.login", result="throttled"))
            await db.commit()
            raise APIError(429, "login_rate_limited", "Too many login attempts", True)
    user = await db.scalar(
        select(User).where(User.normalized_login == normalized).with_for_update()
    )
    valid, upgraded = await run_in_threadpool(
        password_hasher.verify_and_update,
        body.password,
        user.password_hash if user else DUMMY_PASSWORD_HASH,
    )
    if not valid or user is None or user.disabled:
        for key in keys:
            await increment(db, key)
        db.add(AuditEvent(action="auth.login", result="rejected"))
        await db.commit()
        raise APIError(401, "invalid_credentials", "Invalid login or password")
    if upgraded:
        user.password_hash = upgraded
    session.revoked_at = utcnow()
    token, csrf_token = new_secret(), new_secret()
    now = utcnow()
    db.add(
        Session(
            token_hash=digest(token),
            csrf_hash=digest(csrf_token),
            user_id=user.id,
            expires_at=now + timedelta(seconds=get_settings().idle_ttl_seconds),
            absolute_expires_at=now + timedelta(seconds=get_settings().absolute_ttl_seconds),
        )
    )
    db.add(AuditEvent(actor_id=user.id, action="auth.login", result="success"))
    await db.commit()
    cookie(response, token)
    response.headers["Cache-Control"] = "no-store"
    return {
        "user": {"id": str(user.id), "login": user.normalized_login, "role": user.role,
                 "runtime_profile": get_settings().runtime_profile},
        "csrf_token": csrf_token,
    }


@router.get("/me", dependencies=[Depends(api_rate_limit)])
async def me(request: Request, response: Response, db: DB) -> dict[str, str]:
    _, user = await authenticated(request, db)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"id": str(user.id), "login": user.normalized_login, "role": user.role,
            "runtime_profile": get_settings().runtime_profile}


@router.post("/logout", status_code=204, dependencies=[Depends(api_rate_limit)])
async def logout(request: Request, db: DB) -> Response:
    session = await unsafe_session(request, db)
    session.revoked_at = utcnow()
    db.add(AuditEvent(actor_id=session.user_id, action="auth.logout", result="success"))
    await db.commit()
    response = Response(status_code=204)
    response.delete_cookie(
        get_settings().cookie_name,
        path="/",
        secure=get_settings().environment == "production",
        httponly=True,
        samesite="lax",
    )
    return response


async def revoke_user_sessions(db: AsyncSession, user_id: object) -> None:
    await db.execute(
        update(Session)
        .where(Session.user_id == user_id, Session.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
