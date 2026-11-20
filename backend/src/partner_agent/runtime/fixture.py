"""A deterministic, explicitly labelled fixture graph; it never calls a model API."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class FixtureFailure(RuntimeError):
    pass


class FixtureState(TypedDict, total=False):
    input_message_id: str
    question: str
    history: list[dict[str, str]]
    answer: str
    offset: int


def fixture_graph(
    saver: BaseCheckpointSaver[str],
    publish_prefix: Callable[[str], Awaitable[None]],
    *,
    chunk_size: int = 24,
    delay_seconds: float = 0.1,
) -> Any:
    """Persist prepared text before emission; repeated prefix publication is idempotent."""

    async def prepare(state: FixtureState) -> dict[str, Any]:
        question = state["question"]
        if question.startswith("/fixture fail"):
            raise FixtureFailure("Requested offline fixture failure")
        clarification = ""
        if question.startswith("/fixture clarify"):
            value = interrupt(
                {"question": "Выберите вариант для офлайн-проверки", "options": ["А", "Б"]}
            )
            if not isinstance(value, dict) or not isinstance(value.get("answer"), str):
                raise FixtureFailure("Invalid fixture clarification value")
            clarification = " Уточнение: " + value["answer"][:40] + "."
        return {
            "answer": (
                "Офлайн-стенд без LLM. Сообщение принято: "
                + question[:80]
                + "."
                + clarification
                + f" Ходов в контексте ветки: {len(state.get('history', []))}."
            ),
            "offset": 0,
        }

    async def emit(state: FixtureState) -> dict[str, int]:
        target = min(state["offset"] + max(chunk_size, 1), len(state["answer"]))
        await publish_prefix(state["answer"][:target])
        delay = (
            max(delay_seconds, 3.0)
            if state["question"].startswith("/fixture slow")
            else delay_seconds
        )
        if delay > 0:
            await asyncio.sleep(delay)
        return {"offset": target}

    def next_step(state: FixtureState) -> str:
        return "emit" if state["offset"] < len(state["answer"]) else END

    builder = StateGraph(FixtureState)
    builder.add_node("prepare", prepare)
    builder.add_node("emit", emit)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "emit")
    builder.add_conditional_edges("emit", next_step)
    return builder.compile(checkpointer=saver)
