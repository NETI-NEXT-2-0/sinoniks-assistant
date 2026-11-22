import argparse
import asyncio
import hashlib
import json
import os
import secrets
import time
from contextlib import closing
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from stack import ROOT, command, compose

PRIVATE = ROOT / ".local/ci-private"
SOURCE = b"Controlled course EDU-204 costs 100.\nOnly enrolled participants.\n"


def container_host(service: str) -> str:
    container = compose("ps", "-q", service, capture=True).strip()
    address = json.loads(command("docker", "inspect", container, capture=True))[0]
    return next(
        item["IPAddress"]
        for item in address["NetworkSettings"]["Networks"].values()
        if item["IPAddress"]
    )


def environment() -> None:
    values = {
        key: value for key, value in dotenv_values(ROOT / ".env").items() if value is not None
    }
    values["PARTNER_DATABASE_URL"] = (
        make_url(values["PARTNER_DATABASE_URL"])
        .set(host=container_host("postgres"))
        .render_as_string(hide_password=False)
    )
    os.environ.update(values)


def session(account: dict[str, str]) -> tuple[httpx.Client, dict[str, str]]:
    client = httpx.Client(base_url="http://localhost:8080", timeout=20, trust_env=False)
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    result = client.post(
        "/api/v1/auth/login",
        json=account,
        headers={"Origin": "http://localhost:8080", "X-CSRF-Token": token},
    )
    result.raise_for_status()
    return client, {"Origin": "http://localhost:8080", "X-CSRF-Token": result.json()["csrf_token"]}


def wait_version(client: httpx.Client, version: str, field: str, value: object) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/knowledge/versions/{version}")
        response.raise_for_status()
        if response.json().get(field) == value:
            return
        time.sleep(1)
    raise RuntimeError("Document preparation did not reach the expected state")


async def account_user(account: dict[str, str], role: str) -> None:
    from partner_agent.auth.security import password_hasher
    from partner_agent.infra.db import get_engine, session_factory
    from partner_agent.infra.models import User

    async with session_factory()() as db:
        db.add(
            User(
                normalized_login=account["login"],
                password_hash=password_hasher.hash(account["password"]),
                role=role,
                disabled=False,
            )
        )
        await db.commit()
    await get_engine().dispose()


def seed() -> None:
    os.umask(0o077)
    account = {"login": "ci-" + uuid4().hex, "password": secrets.token_urlsafe(24)}
    reader = {"login": "ci-" + uuid4().hex, "password": secrets.token_urlsafe(24)}
    environment()
    asyncio.run(account_user(account, "curator"))
    asyncio.run(account_user(reader, "user"))
    client, headers = session(account)
    with closing(client):
        response = client.post(
            "/api/v1/knowledge/documents", json={"title": "Release probe"}, headers=headers
        )
        response.raise_for_status()
        document = response.json()["id"]
        response = client.post(
            f"/api/v1/knowledge/documents/{document}/versions?expected_revision=1",
            content=SOURCE,
            headers={**headers, "Content-Type": "text/plain", "Idempotency-Key": str(uuid4())},
        )
        response.raise_for_status()
        version = response.json()["id"]
        wait_version(client, version, "review_available", True)
        review = client.get(f"/api/v1/knowledge/versions/{version}/review").json()
        revision = client.get(f"/api/v1/knowledge/documents/{document}").json()["revision"]
        response = client.post(
            f"/api/v1/knowledge/versions/{version}/approve",
            headers=headers,
            json={
                "expected_revision": revision,
                "canonical_sha256": review["canonical_sha256"],
                "block_ids": [item["id"] for item in review["blocks"]],
            },
        )
        response.raise_for_status()
        wait_version(client, version, "status", "ready_for_review")
        revision = client.get(f"/api/v1/knowledge/documents/{document}").json()["revision"]
        response = client.post(
            f"/api/v1/knowledge/versions/{version}/publish",
            headers=headers,
            json={"expected_revision": revision},
        )
        response.raise_for_status()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    (PRIVATE / "probe.json").write_text(
        json.dumps(
            {
                "account": account,
                "reader": reader,
                "document": document,
                "version": version,
                "sha256": hashlib.sha256(SOURCE).hexdigest(),
                "index_sha256": index_hash(document),
            }
        ),
        encoding="utf-8",
    )
    verify()


def index_hash(document: str) -> str:
    from partner_agent.retrieval.index import COLLECTION

    response = httpx.post(
        f"http://{container_host('qdrant')}:6333/collections/{COLLECTION}/points/scroll",
        json={
            "limit": 1000,
            "with_payload": True,
            "with_vector": False,
            "filter": {"must": [{"key": "document_id", "match": {"value": document}}]},
        },
        timeout=20,
        trust_env=False,
    )
    response.raise_for_status()
    points = response.json()["result"]["points"]
    assert points, "Published index points are missing"
    normalized = json.dumps(sorted(points, key=lambda point: point["id"]), sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


def verify() -> None:
    probe = json.loads((PRIVATE / "probe.json").read_text())
    client, headers = session(probe["account"])
    with closing(client):
        document = client.get(f"/api/v1/knowledge/documents/{probe['document']}")
        document.raise_for_status()
        assert document.json()["active_version_id"] == probe["version"]
        result = client.post("/api/v1/knowledge/search", headers=headers, json={"query": "EDU-204"})
        result.raise_for_status()
        assert any(
            item["version_id"] == probe["version"] and "100" in item["excerpt"]
            for item in result.json()["items"]
        )
        original = client.post(
            f"/api/v1/knowledge/versions/{probe['version']}/original/download", headers=headers
        )
        original.raise_for_status()
        assert hashlib.sha256(original.content).hexdigest() == probe["sha256"]
        assert index_hash(probe["document"]) == probe["index_sha256"]
    print("Published version, source hash and authorized excerpts verified")


if __name__ == "__main__":
    if not os.environ.get("COMPOSE_PROJECT_NAME", "").startswith("partner-ci-"):
        raise SystemExit("Isolated test stack required")
    os.environ["COMPOSE_FILE"] = str(ROOT / ".ci-compose.yaml")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seed", "verify"])
    {"seed": seed, "verify": verify}[parser.parse_args().action]()
