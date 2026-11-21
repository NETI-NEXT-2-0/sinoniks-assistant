"""Render a self-contained HTTPS Compose file; never deploy or print credentials.

Run after bootstrap-env.py and setting the production values in a private env
file. No Docker socket is mounted into the API or model worker.
"""

import argparse
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
    "postgres", "garage", "qdrant", "hatchet-migrate", "hatchet-setup", "hatchet-engine",
    "hatchet-dashboard", "migrate", "provision-checkpoints", "api", "agent-worker",
    "relay", "reconciler", "ingest-worker", "caddy",
}


def load_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8-sig").splitlines()
                if "=" in line and not line.lstrip().startswith("#"))


def check_env(values: dict[str, str]) -> list[str]:
    """Return field names/reasons only, never sensitive values."""
    failures = []
    expected = {"PARTNER_ENVIRONMENT": "production",
                "PARTNER_DEVELOPER_DIAGNOSTICS_ENABLED": "false",
                "PARTNER_RUNTIME_PROFILE": "openai_compatible",
                "PARTNER_MODEL_PRICING_DECLARED": "true"}
    for key, value in expected.items():
        if values.get(key, "").lower() != value:
            failures.append(key + " must be " + value)
    origin = urlsplit(values.get("PARTNER_PUBLIC_ORIGIN", ""))
    if (origin.scheme != "https" or not origin.hostname or origin.path not in ("", "/")
            or origin.query or origin.fragment or origin.username or origin.password
            or origin.hostname in {"localhost", "127.0.0.1"}):
        failures.append("PARTNER_PUBLIC_ORIGIN must be the public HTTPS origin")
    if values.get("CADDY_ADDRESS") != origin.netloc:
        failures.append("CADDY_ADDRESS must match the public hostname and optional port")
    for key in ("PARTNER_DATABASE_URL", "HATCHET_DATABASE_URL", "HATCHET_CLIENT_TOKEN",
                "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "PARTNER_MODEL_API_KEY",
                "PARTNER_MODEL_NAME", "PARTNER_MODEL_PROVIDER_VARIANT",
                "PARTNER_MODEL_CATALOG_ID", "PARTNER_MODEL_PRICE_SNAPSHOT"):
        if not values.get(key, "").strip():
            failures.append(key + " is missing")
    endpoint = urlsplit(values.get("PARTNER_MODEL_ENDPOINT", ""))
    if (endpoint.scheme != "https" or not endpoint.hostname
            or endpoint.username or endpoint.password):
        failures.append("PARTNER_MODEL_ENDPOINT must use HTTPS without embedded credentials")
    for key in ("PARTNER_MODEL_REQUEST_CAP_USD", "PARTNER_MODEL_DAILY_CAP_USD"):
        try:
            from decimal import Decimal
            value = Decimal(values.get(key, "0"))
            if not value.is_finite() or value <= 0:
                raise ValueError()
        except (ValueError, ArithmeticError):
            failures.append(key + " must be a positive finite spend limit")
    return failures


def render(revision: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Use the full tested Git SHA")
    source = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = {name: service for name, service in source["services"].items() if name in SERVICES}
    for name, service in services.items():
        if "build" in service:
            service.pop("build")
            service["image"] = f"sinoniks-api:{revision}"
        if name in {"api", "agent-worker", "relay", "reconciler"}:
            service["environment"]["PARTNER_MODEL_TIMEOUT_SECONDS"] = (
                "${PARTNER_MODEL_TIMEOUT_SECONDS:-90}"
            )
        # Keep operator consoles loopback-only and all stores on private networks.
        if name != "caddy" and name != "hatchet-dashboard":
            service.pop("ports", None)
    services["caddy"]["image"] = f"sinoniks-web:{revision}"
    services["caddy"]["ports"] = ["80:80", "443:443", "443:443/udp"]
    services["caddy"]["volumes"] = [volume for volume in services["caddy"]["volumes"]
                                    if not volume.startswith("./")]
    return {"name": "partner-agent", "services": services, "networks": source["networks"],
            "volumes": source["volumes"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, default=ROOT / ".pilot-compose.yaml")
    parser.add_argument("--check-env", action="store_true")
    args = parser.parse_args()
    if args.check_env:
        failures = check_env(load_env(args.env_file))
        if failures:
            parser.exit(2, "Pilot configuration is not ready:\n" + "\n".join(failures) + "\n")
    config = render(args.revision)
    os.umask(0o077)
    with args.output.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(config, stream, sort_keys=False, allow_unicode=True)
    print("Rendered HTTPS Compose for the tested revision; no services were started")


if __name__ == "__main__":
    main()
