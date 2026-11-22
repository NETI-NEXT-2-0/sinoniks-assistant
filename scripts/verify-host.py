#!/usr/bin/env python3
"""Live V1/V2 on local Docker IPs, using only disposable partner_test database.

Run with backend/.venv/bin/python. Credentials stay in process memory; this
script never prints environment values, login prompts, or passwords. It rebuilds
only the application schema in partner_test, leaving the application DB alone.
"""

import asyncio
import importlib.metadata
import os
import pty
import select
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from sqlalchemy import select as sql_select
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def container_ip(service: str) -> str:
    container = subprocess.check_output(
        ["docker", "compose", "ps", "-q", service], cwd=ROOT, text=True
    ).strip()
    if not container:
        raise RuntimeError(f"Start the {service} container first")
    return subprocess.check_output(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            container,
        ],
        text=True,
    ).strip()


def environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            key: value
            for key, value in dotenv_values(ROOT / ".env").items()
            if value is not None
        }
    )
    url = make_url(env["PARTNER_DATABASE_URL"]).set(
        host=container_ip("postgres"), database="partner_test"
    )
    if not url.database or not url.database.endswith("_test"):
        raise RuntimeError("Only a disposable *_test database may be rebuilt")
    env["PARTNER_DATABASE_URL"] = url.render_as_string(hide_password=False)
    env["PARTNER_TEST_DATABASE_URL"] = env["PARTNER_DATABASE_URL"]
    endpoint = f"http://{container_ip('garage')}:3900"
    env.update(
        {
            "PARTNER_ENVIRONMENT": "test",
            "PARTNER_PUBLIC_ORIGIN": "http://localhost:8080",
            "PARTNER_S3_ENDPOINT_URL": endpoint,
            "PARTNER_S3_ACCESS_KEY_ID": env["S3_ACCESS_KEY_ID"],
            "PARTNER_S3_SECRET_ACCESS_KEY": env["S3_SECRET_ACCESS_KEY"],
            "GARAGE_TEST_ENDPOINT": endpoint,
            "GARAGE_TEST_ACCESS_KEY": env["S3_ACCESS_KEY_ID"],
            "GARAGE_TEST_SECRET_KEY": env["S3_SECRET_ACCESS_KEY"],
        }
    )
    return env


def cli(
    env: dict[str, str], command: str, login: str, password: str
) -> tuple[int, bytes]:
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [".venv/bin/python", "-m", "partner_agent.cli", command],
        cwd=BACKEND,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = b""
    deadline = time.monotonic() + 20
    prompts = [
        (b"Login: ", login),
        (b"Password (12+ characters): ", password),
        (b"Confirm password: ", password),
    ]
    sent = 0
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.25)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if sent < len(prompts) and prompts[sent][0] in output:
                    os.write(master, (prompts[sent][1] + "\n").encode())
                    sent += 1
            if process.poll() is not None:
                break
        return process.wait(timeout=5), output
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()


def verify_cli(env: dict[str, str]) -> None:
    # Synthetic, local-test passwords are deliberately public fixtures, never account secrets.
    code, output = cli(
        env, "first-admin", "cli-test-admin", "synthetic-cli-password-123"
    )
    assert code == 0 and b"Completed" in output, "CLI bootstrap failed"
    assert b"synthetic-cli-password-123" not in output, "TTY echoed password"
    code, output = cli(
        env, "first-admin", "cli-test-second", "synthetic-second-password-123"
    )
    assert code != 0 and b"already exists" in output, "CLI allowed second bootstrap"
    code, output = cli(
        env, "reset-password", "cli-test-admin", "synthetic-reset-password-123"
    )
    assert code == 0 and b"Completed" in output, "CLI reset failed"
    assert b"synthetic-reset-password-123" not in output, "TTY echoed reset password"

    os.environ.update(env)
    from partner_agent.auth.security import password_hasher
    from partner_agent.infra.db import get_engine, session_factory
    from partner_agent.infra.models import User

    async def check_password() -> None:
        async with session_factory()() as db:
            user = await db.scalar(
                sql_select(User).where(User.normalized_login == "cli-test-admin")
            )
            assert user is not None and user.role == "admin"
            assert password_hasher.verify(
                "synthetic-reset-password-123", user.password_hash
            )
            assert not password_hasher.verify(
                "synthetic-cli-password-123", user.password_hash
            )
        await get_engine().dispose()

    asyncio.run(check_password())
    print(
        "CLI: real PTY bootstrap/reset/second-bootstrap denial/no password echo passed"
    )


def main() -> None:
    env = environment()
    print("Live verification:", datetime.now(ZoneInfo("Asia/Krasnoyarsk")).isoformat())
    for package in (
        "SQLAlchemy",
        "psycopg",
        "FastAPI",
        "alembic",
        "langgraph",
        "langgraph-checkpoint-postgres",
    ):
        print(f"{package}={importlib.metadata.version(package)}")
    commands = [
        [".venv/bin/alembic", "upgrade", "head"],
        [".venv/bin/alembic", "check"],
        [".venv/bin/alembic", "downgrade", "base"],
        [".venv/bin/alembic", "upgrade", "head"],
        [".venv/bin/alembic", "check"],
        [".venv/bin/python", "-m", "partner_agent.infra.provision"],
        [".venv/bin/alembic", "check"],
    ]
    for command in commands:
        subprocess.run(command, cwd=BACKEND, env=env, check=True)
    verify_cli(env)
    subprocess.run([".venv/bin/pytest", "-q"], cwd=BACKEND, env=env, check=True)
    print("PASS: migration/drift/rollback/checkpoint provision/CLI/auth/Garage")


if __name__ == "__main__":
    main()
