"""Offline fixture index transport; PostgreSQL remains the publication authority."""

import hashlib
import json
from typing import Any
from uuid import UUID

import httpx

COLLECTION = "partner_offline_text_v1"


class IndexConfigurationError(ValueError):
    """The shared fixture collection cannot safely accept this profile."""


class FixtureIndex:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")

    async def _ensure_compatible(self, client: httpx.AsyncClient) -> None:
        exists = await client.get(f"/collections/{COLLECTION}")
        if exists.status_code == 404:
            response = await client.put(f"/collections/{COLLECTION}", json={"vectors": {}})
            if response.status_code not in (200, 409):
                response.raise_for_status()
        else:
            exists.raise_for_status()
        # A concurrent creator can win the race, so always inspect the result.
        config = await client.get(f"/collections/{COLLECTION}")
        config.raise_for_status()
        try:
            params = config.json()["result"]["config"]["params"]
            vectors = params["vectors"]
            sparse_vectors = params.get("sparse_vectors", {})
        except (KeyError, TypeError, ValueError) as exc:
            raise IndexConfigurationError("index_configuration_invalid") from exc
        if vectors != {} or sparse_vectors not in (None, {}):
            raise IndexConfigurationError("index_configuration_incompatible")

    async def ensure_compatible(self) -> None:
        async with httpx.AsyncClient(base_url=self.endpoint, timeout=20) as client:
            await self._ensure_compatible(client)

    async def stage(self, points: list[dict[str, Any]]) -> str:
        if not points:
            raise ValueError("empty_index")
        async with httpx.AsyncClient(base_url=self.endpoint, timeout=20) as client:
            await self._ensure_compatible(client)
            response = await client.put(
                f"/collections/{COLLECTION}/points?wait=true", json={"points": points}
            )
            response.raise_for_status()
            response = await client.post(
                f"/collections/{COLLECTION}/points",
                json={
                    "ids": [p["id"] for p in points],
                    "with_payload": True,
                    "with_vector": False,
                },
            )
            response.raise_for_status()
            actual = {str(p["id"]): p["payload"] for p in response.json()["result"]}
            expected = {str(p["id"]): p["payload"] for p in points}
            if actual != expected:
                raise ValueError("index_verification_mismatch")
            return hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()

    async def delete_version(self, version_id: UUID) -> None:
        async with httpx.AsyncClient(base_url=self.endpoint, timeout=20) as client:
            selector = {"must": [{"key": "version_id", "match": {"value": str(version_id)}}]}
            response = await client.post(
                f"/collections/{COLLECTION}/points/delete?wait=true",
                json={"filter": selector},
            )
            if response.status_code != 404:
                response.raise_for_status()
                remaining = await client.post(
                    f"/collections/{COLLECTION}/points/scroll",
                    json={"filter": selector, "limit": 1, "with_payload": False,
                        "with_vector": False},
                )
                remaining.raise_for_status()
                if remaining.json().get("result", {}).get("points"):
                    raise RuntimeError("index_delete_verification_failed")
