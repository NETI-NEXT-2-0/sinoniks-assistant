"""Checkpoint writes and application ownership markers share one PostgreSQL transaction."""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from partner_agent.runs.service import Ownership, StaleOwnership


class FencedSaver(BaseCheckpointSaver[str]):
    """Owns a dedicated connection; callers use async operations only.

    No pools or externally managed pipelines are accepted. The outer mutex also
    serializes reads, since the delegate's cursor lock cannot protect our guard.
    """

    def __init__(
        self,
        connection: AsyncConnection[DictRow],
        ownership: Ownership,
        input_message_id: UUID,
        *,
        delegate: AsyncPostgresSaver | None = None,
    ) -> None:
        self.connection = connection
        self.ownership = ownership
        self.input_message_id = input_message_id
        self.delegate = delegate or AsyncPostgresSaver(connection)
        if self.delegate.conn is not connection or self.delegate.pipe is not None:
            raise ValueError(
                "Fenced saver requires the same dedicated connection without a pipeline"
            )
        super().__init__(serde=self.delegate.serde)
        self.mutex = asyncio.Lock()

    def _scope(self, config: RunnableConfig) -> None:
        if config.get("configurable", {}).get("thread_id") != str(self.ownership.run_id):
            raise StaleOwnership("Checkpoint thread does not match the owned run")

    async def _guard(self) -> DictRow:
        owner = self.ownership
        chat_cursor = await self.connection.execute(
            "SELECT id, active_run_id, deleted_at FROM public.chats WHERE id = %s FOR UPDATE",
            (owner.chat_id,),
        )
        chat = await chat_cursor.fetchone()
        if chat is None or chat["deleted_at"] is not None or chat["active_run_id"] != owner.run_id:
            raise StaleOwnership("Run is no longer the active chat head")
        cursor = await self.connection.execute(
            "SELECT r.*, r.lease_expires_at > clock_timestamp() AS lease_valid, "
            "u.disabled AS user_disabled FROM public.runs r "
            "JOIN public.users u ON u.id = r.user_id WHERE r.id = %s FOR UPDATE OF r",
            (owner.run_id,),
        )
        run = await cursor.fetchone()
        if (
            run is None
            or run["chat_id"] != owner.chat_id
            or run["branch_id"] != owner.branch_id
            or run["status"] != "running"
            or run["owner_token"] != owner.owner_token
            or run["fence"] != owner.fence
            or not run["lease_valid"]
            or run["user_disabled"]
        ):
            raise StaleOwnership("Checkpoint writer no longer owns a live run")
        return run

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._scope(config)
        async with self.mutex:
            return await self.delegate.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            raise StaleOwnership("Unscoped checkpoint listing is disabled")
        self._scope(config)
        async with self.mutex:
            async for item in self.delegate.alist(
                config, filter=filter, before=before, limit=limit
            ):
                yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        self._scope(config)
        async with self.mutex, self.connection.transaction():
            run = await self._guard()
            result = await self.delegate.aput(config, checkpoint, metadata, new_versions)
            if metadata.get("source") == "input" and not run["input_applied"]:
                values = checkpoint["channel_values"]
                source = values.get("__start__", values)
                if not isinstance(source, dict) or source.get("input_message_id") != str(
                    self.input_message_id
                ):
                    raise StaleOwnership("Input checkpoint does not match the admitted message")
                await self.connection.execute(
                    "UPDATE public.runs SET input_applied = true, input_checkpoint_id = %s "
                    "WHERE id = %s",
                    (checkpoint["id"], self.ownership.run_id),
                )
            # clock_timestamp(), not transaction-constant now(): serialization can outlive a lease.
            await self._guard()
            return result

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._scope(config)
        async with self.mutex, self.connection.transaction():
            run = await self._guard()
            await self.delegate.aput_writes(config, writes, task_id, task_path)
            for channel, value in writes:
                if channel != "__resume__" or not isinstance(value, dict):
                    continue
                command_id = value.get("resume_command_id")
                if command_id is None:
                    continue
                if str(run["pending_resume_command_id"]) != str(command_id):
                    raise StaleOwnership("Resume command is no longer pending")
                await self.connection.execute(
                    "UPDATE public.resume_commands SET input_applied = true, checkpoint_id = %s "
                    "WHERE id = %s AND run_id = %s",
                    (
                        config["configurable"]["checkpoint_id"],
                        UUID(command_id),
                        self.ownership.run_id,
                    ),
                )
            await self._guard()

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        self._scope(config)
        async with self.mutex:
            return await self.delegate.aget_delta_channel_history(config=config, channels=channels)

    def get_next_version(self, current: str | None, channel: None) -> str:
        return self.delegate.get_next_version(current, channel)

    async def adelete_thread(self, thread_id: str) -> None:
        raise RuntimeError("Checkpoint deletion requires stopped writers and maintenance")
