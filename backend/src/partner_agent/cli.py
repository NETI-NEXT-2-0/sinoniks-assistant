import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.auth.routes import revoke_user_sessions
from partner_agent.auth.security import normalize_login, password_hasher
from partner_agent.infra.db import get_engine, session_factory
from partner_agent.infra.models import AuditEvent, User


def validate_password(password: str) -> None:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must contain 12–1024 characters")


async def first_admin(db: AsyncSession, login: str, password: str) -> User:
    validate_password(password)
    normalized = normalize_login(login)
    await db.execute(text("SELECT pg_advisory_xact_lock(8642917501)"))
    if await db.scalar(select(User.id).where(User.role.in_(MANAGER_ROLES)).limit(1)) is not None:
        raise ValueError("An administrator already exists; bootstrap is disabled")
    if await db.scalar(select(User.id).where(User.normalized_login == normalized)) is not None:
        raise ValueError("Login already exists")
    user = User(
        normalized_login=normalized,
        password_hash=await run_in_threadpool(password_hasher.hash, password),
        role="admin",
    )
    db.add(user)
    await db.flush()
    db.add(AuditEvent(actor_id=user.id, action="admin.bootstrap", result="success"))
    await db.commit()
    return user


async def reset_password(db: AsyncSession, login: str, password: str) -> None:
    validate_password(password)
    user = await db.scalar(
        select(User).where(User.normalized_login == normalize_login(login)).with_for_update()
    )
    if user is None:
        raise ValueError("User does not exist")
    user.password_hash = await run_in_threadpool(password_hasher.hash, password)
    await revoke_user_sessions(db, user.id)
    db.add(AuditEvent(actor_id=user.id, action="admin.password_reset", result="success"))
    await db.commit()


async def execute(command: str, login: str, password: str) -> None:
    try:
        async with session_factory()() as db:
            if command == "first-admin":
                await first_admin(db, login, password)
            else:
                await reset_password(db, login, password)
    finally:
        await get_engine().dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local identity administration; no default password"
    )
    parser.add_argument("command", choices=["first-admin", "reset-password"])
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("An interactive terminal is required; passwords are never CLI arguments")
    login = input("Login: ")
    password = getpass.getpass("Password (12+ characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        parser.error("Passwords do not match")
    try:
        asyncio.run(
            execute(args.command, login, password), loop_factory=asyncio.SelectorEventLoop
        )
    except ValueError as exc:
        parser.error(str(exc))
    except SQLAlchemyError:
        parser.error("Database operation failed; check connectivity and migrations")
    print("Completed")


if __name__ == "__main__":
    main()
