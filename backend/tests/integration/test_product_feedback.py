"""Real PostgreSQL ownership, explicit disclosure and retry semantics."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_chat_data_plane import complete, new_chat, submit
from test_chat_data_plane import plane as plane

from partner_agent.infra.db import session_factory
from partner_agent.infra.models_feedback import ProductFeedback

pytestmark = pytest.mark.integration


async def test_feedback_is_explicit_private_idempotent_and_manager_visible(plane):
    client, headers, _ = await plane()
    admin, admin_headers, _ = await plane("admin")
    stranger, stranger_headers, _ = await plane()
    chat = await new_chat(client, headers)
    path = f"/api/v1/chats/{chat['id']}/feedback"
    assert (await client.get(path)).json() == {"feedback": None}
    response = await submit(client, headers, chat)
    run_id = response.json()["run_id"]
    body = {"id": str(uuid4()), "run_id": run_id, "rating": 4, "comment": "Удобнее объяснять ICAP"}
    assert (await client.post(path, headers=headers, json=body)).status_code == 422
    await complete(UUID(run_id))
    assert (await client.post(path, json=body)).status_code == 403
    assert (await stranger.post(path, headers=stranger_headers, json=body)).status_code == 404
    assert (await admin.post(path, headers=admin_headers, json=body)).status_code == 404
    assert (await client.get("/api/v1/admin/feedback")).status_code == 403
    for rating in [0, 6, True, "4"]:
        assert (
            await client.post(path, headers=headers, json={**body, "rating": rating})
        ).status_code == 422
    saved = await client.post(path, headers=headers, json=body)
    assert saved.status_code == 200, saved.text
    assert (await client.post(path, headers=headers, json=body)).json() == saved.json()
    assert (
        await client.post(path, headers=headers, json={**body, "comment": "перезаписать"})
    ).status_code == 409
    listing = (await admin.get("/api/v1/admin/feedback")).json()["items"]
    row = next(x for x in listing if x["id"] == body["id"])
    assert row["rating"] == 4 and row["comment"] == body["comment"]
    assert set(row) == {"id", "rating", "comment", "author", "created_at"}
    assert (await admin.get(f"/api/v1/chats/{chat['id']}")).status_code == 404
    async with session_factory()() as db:
        assert (
            len(
                (
                    await db.scalars(
                        select(ProductFeedback).where(ProductFeedback.chat_id == UUID(chat["id"]))
                    )
                ).all()
            )
            == 1
        )
