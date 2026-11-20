"""Local queued answers use the same immutable completion fence, never a provider."""

import asyncio
from contextlib import suppress

from partner_agent.design_service import context_revision
from partner_agent.infra.db import session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_designs import RunDesignContext
from partner_agent.infra.models_stage2 import Run
from partner_agent.infra.settings import get_settings
from partner_agent.project_answers import ProjectAnswer19, apply_answer
from partner_agent.runs.service import StaleOwnership
from partner_agent.runtime.context import project_evidence
from partner_agent.runtime.lifecycle import Claim, complete, heartbeat
from partner_agent.sinoniks_contract19 import Requirements19


async def execute_project_answer(claim: Claim) -> dict[str, str]:
    run_id = claim.ownership.run_id
    metadata = {
        "profile": get_settings().runtime_profile,
        "execution": "local_project_answer",
        "provider_calls": 0,
        "project_answer": claim.project_answer,
    }
    lost = asyncio.Event()
    parent = asyncio.current_task()

    async def renew() -> None:
        try:
            while True:
                await asyncio.sleep(get_settings().runtime_heartbeat_seconds)
                await heartbeat(claim.ownership)
        except asyncio.CancelledError:
            raise
        except Exception:
            lost.set()
            if parent is not None:
                parent.cancel()

    renewal = asyncio.create_task(renew())
    try:
        async with asyncio.timeout(claim.remaining_seconds):
            answer = ProjectAnswer19.model_validate(claim.project_answer)
            async with session_factory()() as db:
                run = await db.get(Run, run_id)
                actor = await db.get(User, claim.user_id)
                if (
                    run is None
                    or actor is None
                    or actor.disabled
                    or run.request_mode == "reference"
                ):
                    raise APIError(403, "access_revoked", "Project answer unavailable")
                context = await db.get(RunDesignContext, run_id)
                if context is None:
                    raise APIError(409, "design_context_invalid", "Missing project context")
                project = await context_revision(db, run.chat_id, context)
                if project is None or project.id != answer.base_revision_id:
                    raise APIError(409, "design_revision_conflict", "Reload before answering")
                if project.input.get("schema_version") != 3:
                    raise APIError(409, "project_answer_version_invalid", "Use requirements 1.9")
                evidence = await project_evidence(db, actor, run.chat_id, project)
                conditions = apply_answer(Requirements19.model_validate(project.input), answer)
                await db.commit()
            status = await complete(
                claim.ownership,
                "succeeded",
                {**metadata, "answer_mode": "design"},
                design_input=conditions,
                design_parent_id=answer.base_revision_id,
                evidence=evidence,
            )
    except StaleOwnership:
        status = "ownership_lost"
    except asyncio.CancelledError:
        if not lost.is_set():
            raise
        status = "ownership_lost"
    except Exception as error:
        code = (
            "runtime_timeout"
            if isinstance(error, TimeoutError)
            else getattr(error, "code", "project_answer_execution_failed")
        )
        try:
            status = await complete(claim.ownership, "failed", {**metadata, "code": code})
        except (StaleOwnership, APIError):
            status = "ownership_lost"
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
    return {"status": status, "run_id": str(run_id)}
