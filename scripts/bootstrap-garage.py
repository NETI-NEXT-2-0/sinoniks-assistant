#!/usr/bin/env python3
"""Create application Garage buckets/grants after single-node startup.

Run from the repository root after scripts/bootstrap-env.py and `docker compose up
-d garage`. Reads generated .env values as data; never sources shell commands.
"""

import os
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    values: dict[str, str] = {}
    for line in (root / ".env").read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip("\"'")
    values.update(os.environ)
    key = values.get("S3_ACCESS_KEY_ID") or values.get("GARAGE_DEFAULT_ACCESS_KEY")
    if not key:
        raise SystemExit("S3_ACCESS_KEY_ID is required; run scripts/bootstrap-env.py")
    command = ["docker", "compose", "exec", "-T", "garage", "/garage"]
    for bucket in (
        values.get("S3_ORIGINALS_BUCKET", "originals"),
        values.get("S3_DERIVED_BUCKET", "derived"),
    ):
        probe = subprocess.run(
            [*command, "bucket", "info", bucket],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode:
            subprocess.run(
                [*command, "bucket", "create", bucket],
                cwd=root,
                stdout=subprocess.DEVNULL,
                check=True,
            )
        subprocess.run(
            [*command, "bucket", "allow", "--read", "--write", "--key", key, bucket],
            cwd=root,
            stdout=subprocess.DEVNULL,
            check=True,
        )
    print("Garage private application buckets provisioned.")


if __name__ == "__main__":
    main()
