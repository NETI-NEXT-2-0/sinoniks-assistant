"""Prepare the disposable test DB before running the real integration suite."""

import os
import subprocess
import sys

url = os.environ["PARTNER_TEST_DATABASE_URL"]
if not url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
    raise SystemExit("Refusing migrations: test database name must end in _test")
os.environ["PARTNER_DATABASE_URL"] = url
subprocess.run(["alembic", "upgrade", "head"], check=True)
subprocess.run(["python", "-m", "partner_agent.infra.provision"], check=True)
raise SystemExit(subprocess.run(["pytest", "-q", *sys.argv[1:]], check=False).returncode)
