#!/usr/bin/env python3
"""Run inside API through Caddy; verify forged XFF cannot change the throttle IP."""

import hashlib
import json
import os
import socket
from uuid import uuid4

import httpx
import psycopg

with socket.create_connection(("caddy", 80), timeout=5) as connection:
    peer = connection.getsockname()[0]
forged = "198.51.100.77"


def counter(database: psycopg.Connection, address: str) -> int:
    key = "ip:" + hashlib.sha256(address.encode()).hexdigest()
    row = database.execute(
        "SELECT COALESCE(SUM(count), 0) FROM rate_limits WHERE key = %s "
        "AND window_start >= date_trunc('minute', clock_timestamp()) - interval '1 minute'",
        (key,),
    ).fetchone()
    assert row is not None
    return int(row[0])


url = os.environ["PARTNER_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://", 1
)
with psycopg.connect(url, autocommit=True, connect_timeout=3) as database:
    before_peer = counter(database, peer)
    before_forged = counter(database, forged)
    with httpx.Client(base_url="http://caddy", timeout=10, trust_env=False) as client:
        csrf = client.get("/api/v1/auth/csrf", headers={"X-Forwarded-For": forged})
        assert csrf.status_code == 200
        response = client.post(
            "/api/v1/auth/login",
            headers={
                "Origin": os.environ["PARTNER_PUBLIC_ORIGIN"],
                "X-CSRF-Token": csrf.json()["csrf_token"],
                "X-Forwarded-For": forged,
            },
            json={"login": "proxy-smoke-" + uuid4().hex, "password": uuid4().hex},
        )
        assert response.status_code == 401, response.status_code
    assert counter(database, peer) == before_peer + 1, (
        "Throttle must use Caddy's observed client"
    )
    assert counter(database, forged) == before_forged, (
        "Forged XFF reached the throttle identity"
    )
print(
    json.dumps(
        {"observed_client_ip": peer, "forged_xff_ignored": True, "status": "passed"}
    )
)
