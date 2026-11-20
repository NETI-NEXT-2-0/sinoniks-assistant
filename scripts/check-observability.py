#!/usr/bin/env python3
"""Offline infrastructure smoke; invoke inside the API container on private network.

  docker compose exec -T api python < scripts/check-observability.py

Only synthetic bootstrap data is queried. No content/secrets are printed.
"""

import json
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen


def fetch(url):
    with urlopen(url, timeout=5) as response:
        return response.status, response.read().decode()


urls = {
    "alloy": "http://alloy:12345/-/ready",
    "alloy_components": "http://alloy:12345/-/healthy",
    "prometheus": "http://prometheus:9090/-/ready",
    "loki": "http://loki:3100/ready",
    "grafana": "http://grafana:3000/api/health",
    "phoenix": "http://phoenix:6006/readyz",
}
results = {}
for name, url in urls.items():
    deadline = time.monotonic() + 60
    while True:
        try:
            status, _ = fetch(url)
            assert status == 200
            results[name] = "ready"
            break
        except (OSError, AssertionError):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{name} did not become ready") from None
            time.sleep(2)

_, body = fetch("http://prometheus:9090/api/v1/status/flags")
flags = json.loads(body)["data"]
assert flags["storage.tsdb.retention.time"] in ("7d", "1w")
assert flags["storage.tsdb.retention.size"] in ("3GB", "3GiB")
results["prometheus_retention"] = "7d/3GiB"
_, config = fetch("http://loki:3100/config")
assert "retention_enabled: true" in config
assert "delete_request_store: filesystem" in config
assert "  retention_period: 3d\n" in config
results["loki_retention"] = "72h/compactor"

for name, url in {
    "phoenix_auth": "http://phoenix:6006/v1/projects",
    "grafana_auth": "http://grafana:3000/api/datasources",
}.items():
    try:
        fetch(url)
    except HTTPError as error:
        assert error.code in (401, 403), (name, error.code)
        results[name] = "anonymous_denied"
    else:
        raise AssertionError(f"{name}: anonymous API unexpectedly accessible")

for name, url, query in (
    ("alloy_metrics", "http://prometheus:9090/api/v1/query", 'up{service="alloy"}'),
    (
        "alloy_loki",
        "http://loki:3100/loki/api/v1/query_range",
        '{job="bootstrap",pipeline="sanitized-v1"}',
    ),
):
    deadline = time.monotonic() + 60
    while True:
        _, body = fetch(url + "?" + urlencode({"query": query}))
        data = json.loads(body)
        values = data["data"]["result"]
        if values:
            if name == "alloy_metrics":
                assert values[0]["value"][1] == "1"
            else:
                for stream in values:
                    for _, line in stream["values"]:
                        assert line == "technical.bootstrap", (
                            "Unsanitized bootstrap log"
                        )
            results[name] = "verified"
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{name}: no stored telemetry")
        time.sleep(2)
print(json.dumps(results, sort_keys=True))
