#!/usr/bin/env python3
"""Verify random bootstrap credentials through loopback admin interfaces.

Run on the host after observability startup; reads ignored .env without printing
credentials, cookies or response bodies. Never enables an external provider.
"""

import base64
import http.cookiejar
import json
from pathlib import Path
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

root = Path(__file__).resolve().parents[1]
values = {}
for line in (root / ".env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        values[key] = value.strip().strip("\"'")

basic = base64.b64encode(
    ("operator:" + values["GRAFANA_ADMIN_PASSWORD"]).encode()
).decode()
request = Request(
    "http://127.0.0.1:13000/api/user",
    headers={"Authorization": "Basic " + basic},
)
with urlopen(request, timeout=10) as response:
    user = json.load(response)
    assert user["login"] == "operator"
    assert user["isGrafanaAdmin"] is True

opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
request = Request(
    "http://127.0.0.1:16006/auth/login",
    data=json.dumps(
        {
            "email": "admin@localhost",
            "password": values["PHOENIX_ADMIN_PASSWORD"],
        }
    ).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with opener.open(request, timeout=10) as response:
    assert response.status == 204
request = Request("http://127.0.0.1:16006/v1/projects")
with opener.open(request, timeout=10) as response:
    assert response.status == 200
request = Request("http://127.0.0.1:16006/auth/logout", method="GET")
with opener.open(request, timeout=10) as response:
    assert response.status in (200, 204)
print(
    "Grafana and Phoenix: random bootstrap admin credentials verified; session logged out."
)
