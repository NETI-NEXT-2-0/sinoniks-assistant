#!/usr/bin/env python3
"""Real worker restart gate; synthetic UUID-scoped run, automatic takeover disabled."""

import asyncio
import json
import subprocess
import time
from uuid import uuid4

from sqlalchemy import delete

from partner_agent.infra.db import session_factory
from partner_agent.infra.models import User
from partner_agent.infra.models_stage2 import Chat, ChatBranch, Run
from partner_agent.runs.service import admit_run, finish_run, lock_run


async def check():
    user_id = uuid4()
    run_id = None
    restarted = False
    async with session_factory()() as db:
        user = User(
            id=user_id,
            normalized_login="restart-" + user_id.hex,
            password_hash="unused-synthetic",
            role="user",
            disabled=False,
        )
        db.add(user)
        await db.flush()
        chat = Chat(id=uuid4(), owner_id=user_id)
        db.add(chat)
        await db.flush()
        branch = ChatBranch(id=uuid4(), chat_id=chat.id)
        db.add(branch)
        await db.flush()
        chat.default_branch_id = branch.id
        run = await admit_run(db, chat, branch, user, "/fixture slow restart recovery")
        run_id = run.id
        await db.commit()

    async def state():
        async with session_factory()() as db:
            run = await db.get(Run, run_id)
            return run.status, run.partial_text, run.fence, run.outcome or {}

    async def until(predicate, timeout=65):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = await state()
            if predicate(current):
                return current
            await asyncio.sleep(0.25)
        raise RuntimeError("Worker restart gate timed out")

    try:
        before = await until(lambda s: s[0] == "running" and bool(s[1]))
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "kill", "-s", "SIGKILL", "agent-worker"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "up", "-d", "--no-deps", "agent-worker"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        restarted = True
        after = await until(lambda s: s[3].get("recovery_required") is True)
        assert after[0] == "running" and after[2] == before[2]
        assert after[1].startswith(before[1])
        async with session_factory()() as db:
            chat, run = await lock_run(db, run_id)
            assert run.assistant_message_id is None
            await finish_run(db, chat, run, "cancelled", {"code": "restart_gate_cleanup"})
            await db.commit()
        print(
            json.dumps(
                {
                    "worker_restarted": restarted,
                    "manual_recovery_visible": True,
                    "no_automatic_takeover": True,
                    "partial_preserved": True,
                }
            )
        )
    finally:
        if not restarted:
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "compose", "up", "-d", "--no-deps", "agent-worker"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        async with session_factory()() as db:
            if run_id is not None:
                chat, run = await lock_run(db, run_id)
                await finish_run(db, chat, run, "cancelled", {"code": "restart_gate_cleanup"})
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


if __name__ == "__main__":
    import os
    import runpy
    from pathlib import Path

    context = runpy.run_path(str(Path(__file__).with_name("verify-host.py")))
    env = context["environment"]()
    # Use application DB because real Compose workers use it; cleanup is UUID-scoped.
    from sqlalchemy.engine import make_url

    app_name = make_url(
        context["dotenv_values"](context["ROOT"] / ".env")["PARTNER_DATABASE_URL"]
    ).database
    env["PARTNER_DATABASE_URL"] = (
        make_url(env["PARTNER_DATABASE_URL"])
        .set(database=app_name)
        .render_as_string(hide_password=False)
    )
    os.environ.update(env)
    asyncio.run(check())
