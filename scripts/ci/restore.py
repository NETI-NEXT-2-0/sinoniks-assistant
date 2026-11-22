import json
import os
import subprocess
import sys

import yaml
from stack import CONFIG, ROOT, command, compose

PRIVATE = ROOT / ".local/ci-private"
WRITERS = (
    "api",
    "agent-worker",
    "relay",
    "reconciler",
    "ingest-worker",
    "hatchet-engine",
    "hatchet-dashboard",
    "garage",
    "qdrant",
)
STORES = {
    "garage": {"/var/lib/garage/meta": "garage_metadata", "/var/lib/garage/data": "garage_data"},
    "qdrant": {"/qdrant/storage": "qdrant_data"},
}


def transfer(volume: str, name: str, restore: bool) -> None:
    command(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0",
        "--mount",
        f"type=volume,source={volume},target=/data" + ("" if restore else ",readonly"),
        "--mount",
        f"type=bind,source={PRIVATE},target=/backup",
        "--mount",
        f"type=bind,source={ROOT / 'scripts/ci/volume-copy.py'},target=/transfer.py,readonly",
        "--entrypoint",
        "python",
        "partner-agent-api:stage1",
        "/transfer.py",
        "restore" if restore else "save",
        name,
    )


def main() -> None:
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if not project.startswith("partner-ci-"):
        raise SystemExit("Isolated test stack required")
    os.environ["COMPOSE_FILE"] = str(CONFIG)
    compose("stop", "--timeout", "30", *WRITERS)
    for database in ("partner", "hatchet", "phoenix"):
        with (PRIVATE / f"{database}.dump").open("wb") as output:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    database,
                    "-Fc",
                    database,
                ],
                cwd=ROOT,
                check=True,
                stdout=output,
            )
    config = yaml.safe_load(CONFIG.read_text())
    for service, destinations in STORES.items():
        identifier = compose("ps", "--all", "-q", service, capture=True).strip()
        mounts = json.loads(command("docker", "inspect", identifier, capture=True))[0]["Mounts"]
        for destination, key in destinations.items():
            source = next(item["Name"] for item in mounts if item["Destination"] == destination)
            if not source.startswith(project + "_"):
                raise RuntimeError("Unexpected source volume")
            transfer(source, key, False)
            restored = project + "-restored-" + key
            command("docker", "volume", "create", restored)
            transfer(restored, key, True)
            config["volumes"]["restored_" + key] = {"name": restored}
            config["services"][service]["volumes"] = [
                item.replace(key + ":", "restored_" + key + ":", 1)
                for item in config["services"][service]["volumes"]
            ]
    config["volumes"]["restored_postgres"] = {"name": project + "-restored-postgres"}
    config["services"]["postgres"]["volumes"] = [
        item.replace("postgres_data:", "restored_postgres:", 1)
        for item in config["services"]["postgres"]["volumes"]
    ]
    CONFIG.write_text(yaml.safe_dump(config), encoding="utf-8")
    compose(
        "up",
        "-d",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "180",
        "postgres",
        "garage",
        "qdrant",
    )
    for database in ("partner", "hatchet", "phoenix"):
        with (PRIVATE / f"{database}.dump").open("rb") as source:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "-U",
                    database,
                    "-d",
                    database,
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                ],
                cwd=ROOT,
                check=True,
                stdin=source,
            )
    compose("up", "-d", "--wait", "--wait-timeout", "180", "caddy", *WRITERS)
    command(sys.executable, "scripts/smoke-http.py")
    command(sys.executable, "scripts/ci/application-probe.py", "verify")
    command(sys.executable, "scripts/ci/browser.py")
    print("Fresh PostgreSQL, Garage and Qdrant volumes restored and verified")


if __name__ == "__main__":
    main()
