import argparse
import os
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory

from stack import CONFIG, ROOT, command


def browser() -> None:
    command("uv", "pip", "install", "--python", sys.executable, "playwright==1.65.2")
    command(sys.executable, "-m", "playwright", "install", "--with-deps", "chromium")
    command(sys.executable, "scripts/ci/application-probe.py", "seed")
    command(sys.executable, "scripts/ci/browser.py")
    command(sys.executable, "scripts/ci/browser_accounting.py")
    command(sys.executable, "scripts/ci/browser_diagram.py")
    command(sys.executable, "scripts/ci/browser_requirements.py")


def conversion() -> None:
    command("uv", "sync", "--project", "parser", "--locked")
    command("docker", "build", "-f", "parser/Dockerfile", "-t", "partner-agent-parser:stage3", ".")
    command(
        "docker",
        "build",
        "-f",
        "infra/Dockerfile.parser-coordinator",
        "-t",
        "partner-ci-coordinator:local",
        ".",
    )
    command(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/parser-work:rw,noexec,nosuid,size=32m",
        "--entrypoint",
        "/app/.venv/bin/python",
        "partner-ci-coordinator:local",
        "-m",
        "partner_agent.documents.parser_host",
        "--help",
    )
    converter = str(ROOT / "parser/.venv/bin/python")
    command(converter, "scripts/provision-parser-models.py", ".local/parser-models")
    command(converter, "scripts/check-parser-corpus.py")
    command(converter, "scripts/check-parser-limits.py")
    command(sys.executable, "scripts/check-parser-timeout.py")
    environment = runpy.run_path(str(ROOT / "scripts/verify-host.py"))
    env = environment["environment"]()
    env["PARTNER_QDRANT_URL"] = "http://" + environment["container_ip"]("qdrant") + ":6333"
    env["PARTNER_PARSER_INTEGRATION"] = "1"
    env["PARTNER_PARSER_MODELS"] = str(ROOT / ".local/parser-models")
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "partner_agent.infra.provision"], cwd=ROOT, env=env, check=True
    )
    with TemporaryDirectory(prefix="partner-parser-corpus-") as corpus:
        command(converter, "scripts/generate-parser-acceptance.py", corpus)
        env["PARTNER_COMPLEX_PARSER_CORPUS"] = corpus
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                "backend/pyproject.toml",
                "-q",
                "backend/tests",
                "-m",
                "parser",
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )


if __name__ == "__main__":
    if not os.environ.get("COMPOSE_PROJECT_NAME", "").startswith("partner-ci-"):
        raise SystemExit("Isolated test stack required")
    os.environ["COMPOSE_FILE"] = str(CONFIG)
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["browser", "recovery", "conversion"])
    phase = parser.parse_args().phase
    if phase == "recovery":
        command(sys.executable, "scripts/ci/restore.py")
    else:
        {"browser": browser, "conversion": conversion}[phase]()
