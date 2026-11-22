#!/usr/bin/env python3
"""Run inside API: docker compose exec -T api python < scripts/check-infra.py."""

import argparse
import json
import os
from urllib.request import urlopen

import psycopg

parser = argparse.ArgumentParser()
parser.add_argument("--observability", action="store_true")
args = parser.parse_args()
urls = {
    "api_live": "http://api:8000/health/live",
    "api_ready": "http://api:8000/health/ready",
    "qdrant_ready": "http://qdrant:6333/readyz",
    "hatchet_live": "http://hatchet-engine:8733/live",
    "hatchet_ready": "http://hatchet-engine:8733/ready",
    "hatchet_dashboard": "http://hatchet-dashboard/",
}
if args.observability:
    urls.update(
        {
            "alloy_ready": "http://alloy:12345/-/ready",
            "prometheus_ready": "http://prometheus:9090/-/ready",
            "loki_ready": "http://loki:3100/ready",
            "grafana_health": "http://grafana:3000/api/health",
            "phoenix_health": "http://phoenix:6006/healthz",
            "phoenix_ready": "http://phoenix:6006/readyz",
        }
    )
checks = {}
for name, url in urls.items():
    with urlopen(url, timeout=10) as response:
        assert response.status == 200, (name, response.status)
        checks[name] = response.status

# Use the actual application role; metadata alone is insufficient isolation evidence.
connection_url = os.environ["PARTNER_DATABASE_URL"].replace(
    "postgresql+psycopg://", "postgresql://", 1
)
with psycopg.connect(connection_url, connect_timeout=3) as connection:
    flags = connection.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    assert flags == (False, False, False), (
        "Application database role has excessive privileges"
    )
for database in ("hatchet", "phoenix"):
    try:
        with psycopg.connect(connection_url, dbname=database, connect_timeout=3):
            pass
    except psycopg.OperationalError as error:
        if error.sqlstate != "42501" and "permission denied for database" not in str(
            error
        ):
            raise RuntimeError(
                f"Unexpected error during database isolation check: {database}"
            ) from None
    else:
        raise RuntimeError(
            f"Application role can connect to foreign database: {database}"
        )
    checks[f"app_role_denied_{database}"] = True
checks["app_role_unprivileged"] = True
print(json.dumps(checks, sort_keys=True))
