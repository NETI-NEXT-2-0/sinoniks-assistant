"""Explicit trusted local operator bridge; API/model workers never receive Docker access."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    environment = {
        "PARTNER_DATABASE_URL": values["PARTNER_DATABASE_URL"],
        "PARTNER_S3_ENDPOINT_URL": values["S3_ENDPOINT_URL"],
        "PARTNER_PARSER_MODEL_VOLUME": "partner-parser-models",
        "PARTNER_PARSER_WORK_VOLUME": "partner-parser-work",
    }
    for name in (
        "ACCESS_KEY_ID",
        "SECRET_ACCESS_KEY",
        "REGION",
        "ORIGINALS_BUCKET",
        "DERIVED_BUCKET",
    ):
        environment["PARTNER_S3_" + name] = values["S3_" + name]
    config = ROOT / ".local/parser-coordinator.env"
    config.parent.mkdir(exist_ok=True)
    os.umask(0o077)
    config.write_text(
        "".join(f"{key}={value}\n" for key, value in environment.items()),
        encoding="utf-8",
    )
    existing = subprocess.run(
        ["docker", "inspect", "partner-parser-coordinator"],
        capture_output=True,
        check=False,
    )
    if existing.returncode == 0:
        print(
            "Coordinator already exists; inspect it before any manual restart. No takeover."
        )
        return
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "partner-parser-coordinator",
            "--network",
            "partner-agent_private",
            "--env-file",
            str(config),
            "--mount",
            f"type=bind,source={ROOT},target=/repo,readonly",
            "--mount",
            "type=volume,source=partner-parser-work,target=/parser-work",
            "--mount",
            "type=volume,source=partner-parser-models,target=/models,readonly",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "partner-agent-parser-coordinator:local",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print(
        "Started trusted local parser coordinator. Secrets not displayed; no auto-restart."
    )


if __name__ == "__main__":
    main()
