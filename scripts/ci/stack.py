import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".ci-compose.yaml"
SERVICES = {
    "postgres",
    "garage",
    "qdrant",
    "hatchet-migrate",
    "hatchet-setup",
    "hatchet-engine",
    "hatchet-dashboard",
    "migrate",
    "provision-checkpoints",
    "api",
    "agent-worker",
    "relay",
    "reconciler",
    "ingest-worker",
    "tests",
    "caddy",
}


def command(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        args, cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout if capture else ""


def compose(*args: str, capture: bool = False) -> str:
    return command("docker", "compose", *args, capture=capture)


def prepare() -> None:
    command(sys.executable, "scripts/bootstrap-env.py")
    config = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = {name: item for name, item in config["services"].items() if name in SERVICES}
    # The browser packs several workflows behind one proxy IP into a single minute.
    # Rate-limit behavior has dedicated auth tests; retain production defaults.
    services["api"]["environment"] = {
        **services["api"]["environment"], "PARTNER_API_REQUESTS_PER_MINUTE": "1000",
    }
    frontend = services["caddy"]
    frontend["image"] = "partner-ci-frontend:local"
    frontend["build"] = {"context": ".", "dockerfile": "infra/Dockerfile.frontend"}
    frontend["volumes"] = [item for item in frontend["volumes"] if not item.startswith("./")]
    services["tests"]["environment"]["PARTNER_QDRANT_URL"] = "http://qdrant:6333"
    services["tests"]["command"] = [
        "python",
        "test-entrypoint.py",
        "--ignore=tests/test_ci_contracts.py",
        "-m",
        "not parser",
    ]
    services["tests"]["depends_on"]["qdrant"] = {"condition": "service_started"}
    CONFIG.write_text(
        yaml.safe_dump(
            {"services": services, "networks": config["networks"], "volumes": config["volumes"]}
        ),
        encoding="utf-8",
    )
    compose("config", "--quiet")


def start() -> None:
    compose("build", "api", "tests", "caddy")
    compose(
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "180",
        "postgres",
        "garage",
        "qdrant",
        "hatchet-dashboard",
        "hatchet-engine",
    )
    command(sys.executable, "scripts/bootstrap-garage.py")
    command(sys.executable, "scripts/bootstrap-hatchet.py")
    compose(
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "180",
        "caddy",
        "agent-worker",
        "relay",
        "reconciler",
        "ingest-worker",
    )
    command(sys.executable, "scripts/smoke-http.py")


def integration() -> None:
    compose("run", "--rm", "--no-deps", "tests")


def diagnostics() -> None:
    report = ROOT / ".local/ci-report"
    report.mkdir(parents=True, exist_ok=True)
    raw = compose("ps", "--all", "--format", "json", capture=True).strip()
    rows = (
        json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines()]
    )
    safe = [
        {key: item.get(key) for key in ("Service", "State", "Health", "ExitCode")} for item in rows
    ]
    (report / "containers.json").write_text(json.dumps(safe, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["prepare", "start", "integration", "diagnostics", "clean"]
    )
    action = parser.parse_args().action
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if not re.fullmatch(r"partner-ci-[a-z0-9-]+", project):
        raise SystemExit("An isolated partner-ci project name is required")
    os.environ["COMPOSE_FILE"] = str(CONFIG)
    if action == "clean":
        compose("down", "--volumes", "--remove-orphans", "--timeout", "20")
    else:
        {
            "prepare": prepare,
            "start": start,
            "integration": integration,
            "diagnostics": diagnostics,
        }[action]()


if __name__ == "__main__":
    main()
