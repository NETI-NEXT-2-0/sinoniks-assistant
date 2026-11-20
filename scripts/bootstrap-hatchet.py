#!/usr/bin/env python3
"""Create a tenant-scoped local scheduler token without exposing it in logs or argv."""

import os
import re
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / ".env"
    content = path.read_text()
    values = dict(
        line.split("=", 1)
        for line in content.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    if values.get("HATCHET_CLIENT_TOKEN"):
        print("Existing local scheduler token retained")
        return
    result = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "hatchet-setup",
            "/hatchet/hatchet-admin",
            "token",
            "create",
            "--config",
            "/hatchet/config",
            "--name",
            "partner-agent-local",
            "--expiresIn",
            "336h",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise SystemExit("Scheduler token bootstrap failed; inspect local service health")
    tokens = re.findall(r"(?m)^([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$", result.stdout)
    if len(tokens) != 1:
        raise SystemExit(
            "Scheduler bootstrap returned an unexpected response; token was not printed"
        )
    additions = {
        "HATCHET_CLIENT_TOKEN": tokens[0],
        "HATCHET_CLIENT_SERVER_URL": "http://hatchet-dashboard:80",
        "HATCHET_CLIENT_HOST_PORT": "hatchet-engine:7070",
        "HATCHET_CLIENT_TLS_STRATEGY": "none",
    }
    content = content.rstrip() + "\n"
    content += "".join(
        f"{key}={value}\n" for key, value in additions.items() if not values.get(key)
    )
    os.umask(0o077)
    temporary = path.with_suffix(".env.tmp")
    with temporary.open("x") as output:
        output.write(content)
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    print("Created private local scheduler token with a 14-day expiration; value not displayed")


if __name__ == "__main__":
    main()
