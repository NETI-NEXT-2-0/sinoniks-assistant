#!/usr/bin/env python3
"""Actual same-origin Caddy/API smoke; no real administrator or paid API required."""

import argparse
from uuid import uuid4

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://localhost:8080")
    args = parser.parse_args()
    origin = args.origin.rstrip("/")
    with httpx.Client(base_url=origin, timeout=15, trust_env=False) as client:
        for path in ("/", "/health/live", "/health/ready"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)
        response = client.get("/api/v1/auth/csrf")
        assert response.status_code == 200
        assert "httponly" in response.headers["set-cookie"].lower()
        assert response.headers["cache-control"] == "no-store"
        token = response.json()["csrf_token"]
        body = {"login": f"smoke-unknown-{uuid4()}", "password": "synthetic-smoke-only"}
        response = client.post("/api/v1/auth/login", json=body)
        assert response.status_code == 403 and response.json()["code"] == "origin_rejected"
        response = client.post("/api/v1/auth/login", json=body, headers={"Origin": origin})
        assert response.status_code == 403 and response.json()["code"] == "csrf_rejected"
        headers = {"Origin": origin, "X-CSRF-Token": token}
        response = client.post("/api/v1/auth/login", json=body, headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"
        assert response.headers["x-request-id"] == response.json()["request_id"]
        assert body["password"] not in response.text
        assert client.get("/api/v1/auth/me").status_code == 401
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401
    print("PASS: Caddy static/live/ready and actual HTTP CSRF/Origin/session/logout")


if __name__ == "__main__":
    main()
