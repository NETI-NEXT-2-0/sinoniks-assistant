"""Validate offline fixture isolation against real Qdrant collections."""

import os
import uuid

import httpx
import pytest

import partner_agent.retrieval.index as fixture_index

pytestmark = [pytest.mark.integration, pytest.mark.storage]


@pytest.mark.parametrize(
    ("vectors", "sparse_vectors", "compatible"),
    [
        ({}, {}, True),
        ({"size": 4, "distance": "Cosine"}, {}, False),
        ({}, {"words": {}}, False),
    ],
)
async def test_fixture_index_rejects_incompatible_collection(
    monkeypatch, vectors, sparse_vectors, compatible
):
    endpoint = os.getenv("PARTNER_QDRANT_URL")
    if not endpoint:
        pytest.skip("PARTNER_QDRANT_URL is required for real Qdrant tests")
    collection = f"partner_test_fixture_{uuid.uuid4().hex}"
    monkeypatch.setattr(fixture_index, "COLLECTION", collection)
    point = {"id": str(uuid.uuid4()), "vector": {}, "payload": {"profile": "fixture-test"}}
    async with httpx.AsyncClient(base_url=endpoint, timeout=20) as client:
        created = await client.put(
            f"/collections/{collection}",
            json={"vectors": vectors, "sparse_vectors": sparse_vectors},
        )
        created.raise_for_status()
        try:
            index = fixture_index.FixtureIndex(endpoint)
            if compatible:
                assert len(await index.stage([point])) == 64
            else:
                with pytest.raises(ValueError, match="index_configuration_incompatible"):
                    await index.stage([point])
            count = await client.post(
                f"/collections/{collection}/points/count", json={"exact": True}
            )
            count.raise_for_status()
            assert count.json()["result"]["count"] == int(compatible)
        finally:
            deleted = await client.delete(f"/collections/{collection}")
            deleted.raise_for_status()


async def test_fixture_index_checks_collection_after_losing_create_race(monkeypatch):
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if len(requests) == 1:
            return httpx.Response(404)
        if len(requests) == 2:
            return httpx.Response(409)
        return httpx.Response(200, json={"result": {"config": {"params": {
            "vectors": {"size": 4, "distance": "Cosine"}
        }}}})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        fixture_index.httpx, "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    point = {"id": str(uuid.uuid4()), "vector": {}, "payload": {}}
    with pytest.raises(ValueError, match="index_configuration_incompatible"):
        await fixture_index.FixtureIndex("http://test.invalid").stage([point])
    assert [method for method, _ in requests] == ["GET", "PUT", "GET"]

async def test_fixture_index_delete_verifies_no_points_remain(monkeypatch):
    requests = []
    version_id = uuid.uuid4()

    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/delete"):
            return httpx.Response(200, json={"result": {"status": "ok"}})
        return httpx.Response(200, json={"result": {"points": [], "next_page_offset": None}})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        fixture_index.httpx, "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    await fixture_index.FixtureIndex("http://test.invalid").delete_version(version_id)
    assert requests == [
        f"/collections/{fixture_index.COLLECTION}/points/delete",
        f"/collections/{fixture_index.COLLECTION}/points/scroll",
    ]
