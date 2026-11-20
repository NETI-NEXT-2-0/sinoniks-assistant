#!/usr/bin/env python3
"""Generate private local credentials once; never overwrite an existing environment."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.umask(0o077)
target = ROOT / ".env"
if target.exists():
    raise SystemExit(".env exists; leaving existing credentials unchanged")
local = ROOT / "infra/local"
local.mkdir(mode=0o700, parents=True, exist_ok=True)
passwords = {
    name: secrets.token_hex(24)
    for name in (
        "POSTGRES_PASSWORD",
        "APP_DB_PASSWORD",
        "HATCHET_DB_PASSWORD",
        "PHOENIX_DB_PASSWORD",
    )
}
values = {
    **passwords,
    "PARTNER_ENVIRONMENT": "development",
    "PARTNER_PUBLIC_ORIGIN": "http://localhost:8080",
    "PARTNER_DATABASE_URL": f"postgresql+psycopg://partner:{passwords['APP_DB_PASSWORD']}@postgres:5432/partner",
    "HATCHET_DATABASE_URL": f"postgres://hatchet:{passwords['HATCHET_DB_PASSWORD']}@postgres:5432/hatchet?sslmode=disable",
    "HATCHET_ADMIN_EMAIL": "operator@partner.local",
    "HATCHET_ADMIN_PASSWORD": "Aa1" + secrets.token_hex(24),
    "GARAGE_DEFAULT_ACCESS_KEY": "GK" + secrets.token_hex(12),
    "GARAGE_DEFAULT_SECRET_KEY": secrets.token_hex(32),
    "GARAGE_DEFAULT_BUCKET": "originals",
    "S3_ENDPOINT_URL": "http://garage:3900",
    "S3_REGION": "garage",
    "S3_ORIGINALS_BUCKET": "originals",
    "S3_DERIVED_BUCKET": "derived",
    "CADDY_ADDRESS": ":80",
    "GRAFANA_ADMIN_PASSWORD": secrets.token_hex(24),
    "PHOENIX_SECRET": "a1" + secrets.token_hex(32),
    "PHOENIX_ADMIN_PASSWORD": "Aa1!" + secrets.token_hex(24),
    "SEARXNG_SECRET": secrets.token_hex(32),
}
values["PHOENIX_SQL_DATABASE_URL"] = (
    f"postgresql://phoenix:{passwords['PHOENIX_DB_PASSWORD']}@postgres:5432/phoenix"
)
values["S3_ACCESS_KEY_ID"] = values["GARAGE_DEFAULT_ACCESS_KEY"]
values["S3_SECRET_ACCESS_KEY"] = values["GARAGE_DEFAULT_SECRET_KEY"]
config = f'''metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "0.0.0.0:3901"
rpc_public_addr = "garage:3901"
rpc_secret = "{secrets.token_hex(32)}"
[s3_api]
s3_region = "garage"
api_bind_addr = "0.0.0.0:3900"
[admin]
api_bind_addr = "0.0.0.0:3903"
admin_token = "{secrets.token_hex(32)}"
metrics_token = "{secrets.token_hex(32)}"
'''
with target.open("x", encoding="utf8") as f:
    f.write("".join(f"{key}={value}\n" for key, value in values.items()))
(local / "garage.toml").write_text(config, encoding="utf8")
print("Created .env and infra/local/garage.toml.")
