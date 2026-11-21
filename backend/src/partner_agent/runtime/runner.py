"""Owned Hatchet execution for real provider requests and isolated fixtures."""

import asyncio
import json
from contextlib import suppress
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from partner_agent.design_service import context_revision, project_readout
from partner_agent.infra.db import session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_designs import RunDesignContext
from partner_agent.infra.models_stage2 import Run
from partner_agent.infra.settings import get_settings
from partner_agent.retrieval.service import search_answer_evidence, validate_evidence_access
from partner_agent.runs.service import StaleOwnership, lock_run, publish_prefix
from partner_agent.runtime.accounting import accounting_scope
from partner_agent.runtime.context import answer_history, project_evidence
from partner_agent.runtime.fenced import FencedSaver
from partner_agent.runtime.fixture import FixtureFailure, fixture_graph
from partner_agent.runtime.ledger import reconcile_daily_usage, reserve_daily_usage
from partner_agent.runtime.lifecycle import claim_run, complete, heartbeat, wait_for_input
from partner_agent.runtime.provider import (
    ProviderError,
    enforce_request_cap,
    estimate_cost_usd,
    reservation_cost_usd,
)
from partner_agent.runtime.rag_provider import (
    ProviderOptions,
    answer_with_provider,
    prepare_request,
)
from partner_agent.runtime.recovery import recovery_message


async def execute_run(
    run_id: UUID, *, dispatch_generation: int, scheduler_id: str | None = None
) -> dict[str, str]:
    claim = await claim_run(run_id, dispatch_generation, scheduler_id)
    if claim is None:
        return {"status": "duplicate_or_ineligible", "run_id": str(run_id)}
    if claim.project_answer is not None:
        from partner_agent.runtime.project_answer import execute_project_answer

        return await execute_project_answer(claim)
    settings = get_settings()
    if settings.runtime_profile != "offline_fixture":
        parent = asyncio.current_task()
        assert parent is not None
        lost = asyncio.Event()
        async def renew_real() -> None:
            try:
                while True:
                    await asyncio.sleep(settings.runtime_heartbeat_seconds)
                    await heartbeat(claim.ownership)
            except asyncio.CancelledError:
                raise
            except Exception:
                lost.set()
                if parent is not None:
                    parent.cancel()
        renewal = asyncio.create_task(renew_real())
        usage: dict[str, int] = {}
        actual_cost: float | None = None
        provider_started = False
        try:
            async with asyncio.timeout(claim.remaining_seconds):
                async with session_factory()() as db:
                    actor = await db.get(User, claim.user_id)
                    if actor is None:
                        raise APIError(403, "access_revoked", "Access revoked")
                    question = (claim.graph_input.get("question", "")
                                if isinstance(claim.graph_input, dict) else "")
                    history, history_sources = await answer_history(db, actor, run_id)
                    await db.commit()
                    from partner_agent.documents.chat_files import attachment_context

                    current_run = await db.get(Run, run_id)
                    assert current_run is not None
                    context = await db.get(RunDesignContext, run_id)
                    if context is None:
                        raise APIError(409, "design_context_invalid", "Project context is missing")
                    project = await context_revision(db, current_run.chat_id, context)
                    project_parent = project.id if project else None
                    if project is not None:
                        try:
                            validated_project_sources = await project_evidence(
                                db, actor, current_run.chat_id, project,
                            )
                        except APIError as error:
                            if (current_run.request_mode != "reference"
                                    or error.code != "source_context_changed"):
                                raise
                            # Reference can still answer a general educational question.
                            # Never expose a snapshot derived from revoked evidence.
                            project, project_parent = None, None
                        else:
                            history_sources = [*history_sources, *validated_project_sources]
                    project_context = None
                    if project is not None:
                        from partner_agent.requirements_service import without_sources

                        project_context = without_sources(project.input)
                    new_project = bool(
                        project is None and current_run.request_mode != "reference"
                        and (current_run.outcome or {}).get("design_input_version") == 3
                    )
                    if new_project:
                        from partner_agent.requirements_service import new_project_context

                        project_context = new_project_context(current_run.chat_id)
                    private_evidence = await attachment_context(
                        db, actor, current_run, str(question)
                    )
                    await db.commit()
                    evidence = await search_answer_evidence(
                        db, actor, str(question), history, limit=8
                    )
                    evidence = [*private_evidence, *evidence]
                    await db.commit()
                    await validate_evidence_access(
                        db, actor, [*evidence, *history_sources], chat_id=claim.ownership.chat_id
                    )
                    calculation = project_readout(project) if project is not None else None
                    prepared = prepare_request(
                        str(question), evidence, history, design_context=project_context,
                        project_calculation=calculation, request_mode=current_run.request_mode,
                        new_project=new_project,
                        context_window_tokens=settings.model_context_window_tokens,
                        max_output_tokens=settings.model_max_output_tokens,
                    )
                    evidence = prepared.evidence
                    estimate = reservation_cost_usd(settings)
                    enforce_request_cap(settings, estimate)
                    reservation_day = await reserve_daily_usage(db, settings, estimate)
                    await db.commit()
                try:
                    options: ProviderOptions = {"history": history, "prepared": prepared}
                    if project_context is not None:
                        options["design_context"] = project_context
                    if current_run.request_mode != "auto":
                        options["request_mode"] = current_run.request_mode
                    with accounting_scope(run_id):
                        provider_started = True
                        result = await answer_with_provider(
                            settings, str(question), evidence, **options,
                        )
                    usage = result["usage"]
                except ProviderError as error:
                    usage = error.usage or {}
                    raise
                finally:
                    actual_cost = estimate_cost_usd(settings, usage)
                    if actual_cost is not None and reservation_day is not None:
                        async with session_factory()() as db:
                            await reconcile_daily_usage(
                                db, settings, reservation_day, estimate, actual_cost
                            )
                            await db.commit()
                if actual_cost is not None:
                    enforce_request_cap(settings, actual_cost)
                answer_mode = result.get("answer_mode", "grounded")
                answer = result["answer"]
                if answer_mode == "project_explanation" and project is not None:
                    answer = f"По сохранённой ревизии №{project.revision}.\n\n{answer}"
                elif (current_run.request_mode == "design" and answer_mode == "insufficient"
                      and project is not None):
                    answer = (
                        "Изменения по этому запросу не сохранены. "
                        f"Запрос выполнен для версии №{project.revision}.\n\n{answer}"
                    )
                success_outcome: dict[str, Any] = {
                    "profile": settings.runtime_profile, "citations": result["citations"],
                    "usage": result["usage"], "estimated_cost_usd": actual_cost,
                    "usage_complete": actual_cost is not None, "answer_mode": answer_mode,
                }
                if settings.developer_diagnostics_enabled and settings.environment != "production":
                    project_counts = {
                        field: len(project_context.get(field) or [])
                        for field in ("networks", "systems", "flows", "actors")
                    } if isinstance(project_context, dict) else None
                    success_outcome["developer_diagnostics"] = {
                        "stage": "completed",
                        "run_id": str(run_id),
                        "request_mode": current_run.request_mode,
                        "answer_mode": answer_mode,
                        "design_returned": result.get("design") is not None,
                        "design_context_supplied": project_context is not None,
                        "design_context_schema_version": (
                            project_context.get("schema_version")
                            if isinstance(project_context, dict) else None
                        ),
                        "design_context_counts": project_counts,
                        "design_context_chars": (
                            len(json.dumps(
                                project_context, ensure_ascii=False, separators=(",", ":"),
                            ))
                            if project_context is not None else 0
                        ),
                        "prepared_message_chars": len(json.dumps(
                            prepared.messages, ensure_ascii=False
                        )),
                        "prepared_message_count": len(prepared.messages),
                        "model_response": result.get("developer_model_response"),
                    }
                status = await complete(
                    claim.ownership, "succeeded",
                    success_outcome, answer,
                    evidence=[*evidence, *history_sources],
                    design_input=result.get("design"), design_parent_id=project_parent,
                    vendor_wishes=result.get("vendor_wishes"),
                )
                return {"status": status, "run_id": str(run_id)}
        except asyncio.CancelledError:
            if lost.is_set():
                return {"status": "ownership_lost", "run_id": str(run_id)}
            raise
        except Exception as error:
            code = (
                "runtime_timeout" if isinstance(error, TimeoutError)
                else getattr(error, "code", "provider_execution_failed")
            )
            failure: dict[str, Any] = {"code": code, "profile": settings.runtime_profile}
            if not provider_started:
                failure["provider_calls"] = 0
            if (settings.developer_diagnostics_enabled and settings.environment != "production"):
                diagnostics = getattr(error, "diagnostics", None)
                failure["developer_diagnostics"] = {
                    "run_id": str(run_id),
                    "exception_type": type(error).__name__,
                    **(diagnostics if isinstance(diagnostics, dict) else
                       {"stage": "runtime", "reason": str(code)}),
                }
            if usage:
                failure.update(usage=usage, estimated_cost_usd=actual_cost,
                               usage_complete=actual_cost is not None)
            message = (recovery_message(str(code))
                       if isinstance(error, (ProviderError, TimeoutError))
                       or (isinstance(error, APIError) and code == "request_mode_violation")
                       else None)
            if message is not None:
                failure.update(answer_mode="insufficient", response_status="needs_clarification",
                               task_completed=False, citations=[])
            try:
                status = await complete(
                    claim.ownership, "succeeded" if message is not None else "failed", failure,
                    text=message, evidence=[] if message is not None else None,
                )
            except (StaleOwnership, APIError):
                status = "ownership_lost"
            return {"status": status, "run_id": str(run_id)}
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
    ownership = claim.ownership
    parent = asyncio.current_task()
    assert parent is not None
    lost = asyncio.Event()

    async def renew() -> None:
        try:
            while True:
                await asyncio.sleep(settings.runtime_heartbeat_seconds)
                await heartbeat(ownership)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Without a confirmed lease we stop computation; no stale publication.
            lost.set()
            parent.cancel()

    async def publish(text: str) -> None:
        async with session_factory()() as db:
            chat, run = await lock_run(db, run_id)
            await publish_prefix(db, chat, run, ownership, text)
            await db.commit()

    renewal = asyncio.create_task(renew())
    try:
        url = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        async with await AsyncConnection.connect(
            url,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            options="-c search_path=langgraph,public",
            connect_timeout=5,
        ) as connection:
            saver = FencedSaver(connection, ownership, claim.input_message_id)
            graph = fixture_graph(
                saver,
                publish,
                chunk_size=settings.runtime_fixture_chunk_size,
                delay_seconds=settings.runtime_fixture_delay_seconds,
            )
            config: dict[str, Any] = {
                "configurable": {"thread_id": str(run_id), "checkpoint_ns": ""},
                "recursion_limit": 12,
            }
            async with asyncio.timeout(claim.remaining_seconds):
                result = await graph.ainvoke(claim.graph_input, config, durability="sync")
                state = await graph.aget_state(config)
                interrupts = [item for task in state.tasks for item in task.interrupts]
                if interrupts:
                    if len(interrupts) != 1:
                        raise FixtureFailure("Fixture supports one pending clarification")
                    item = interrupts[0]
                    value = item.value
                    if not isinstance(value, dict):
                        raise FixtureFailure("Invalid persisted clarification")
                    await wait_for_input(
                        ownership,
                        str(value["question"]),
                        list(value["options"]),
                        str(item.id),
                    )
                    return {"status": "waiting_input", "run_id": str(run_id)}
                answer = result.get("answer")
                if not isinstance(answer, str):
                    raise FixtureFailure("Fixture completed without an answer")
                status = await complete(
                    ownership,
                    "succeeded",
                    {"profile": "offline_fixture", "provider_calls": 0},
                    answer,
                )
                return {"status": status, "run_id": str(run_id)}
    except (StaleOwnership, APIError):
        return {"status": "ownership_lost", "run_id": str(run_id)}
    except asyncio.CancelledError:
        if lost.is_set():
            return {"status": "ownership_lost", "run_id": str(run_id)}
        raise
    except Exception as error:
        # Neither SDK logs nor the application outcome receive original exception/input text.
        code = "runtime_timeout" if isinstance(error, TimeoutError) else "fixture_execution_failed"
        try:
            status = await complete(
                ownership, "failed", {"code": code, "profile": "offline_fixture"}
            )
        except (StaleOwnership, APIError):
            status = "ownership_lost"
        return {"status": status, "run_id": str(run_id)}
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
