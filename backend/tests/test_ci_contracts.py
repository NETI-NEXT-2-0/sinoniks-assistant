import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_SCRIPT = ROOT / "scripts/extract-page-snapshot.py"


def run_snapshot_batch(tmp_path, rows):
    manifest = tmp_path / "sources.csv"
    with manifest.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("title", "topic", "url", "captured_on", "file"))
        writer.writerows(rows)
    command = [sys.executable, str(SNAPSHOT_SCRIPT), "--manifest", "sources.csv", "out"]
    return subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")


def test_snapshot_bundle_keeps_provenance_and_deduplicates_prepared_content(tmp_path):
    page = '<nav>Menu</nav><article><p>Курс, 24 часа.</p><script>Hidden</script></article>'
    snapshot = "Курс, 24 часа.\n".encode()
    (tmp_path / "страница.html").write_text(page, encoding="utf-8")
    (tmp_path / "duplicate.txt").write_bytes(snapshot)
    pdf = b"%PDF-1.7\n%%EOF\n"
    (tmp_path / "catalog.pdf").write_bytes(pdf)
    rows = [
        ("Курс, общий", "Программы", "https://EXAMPLE.org/course", "2026-11-17", "страница.html"),
        ("Зеркало", "Программы", "https://example.org/mirror", "2026-11-17", "duplicate.txt"),
        ("Каталог", "Документы", "https://example.org/catalog", "2026-11-16", "catalog.pdf"),
    ]
    process = run_snapshot_batch(tmp_path, rows)
    assert process.returncode == 0, process.stderr
    out = tmp_path / "out"
    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["snapshot_count"] == 3 and inventory["unique_file_count"] == 2
    assert inventory["snapshots_by_topic"] == {"Программы": 2, "Документы": 1}
    course, mirror, catalog = inventory["snapshots"]
    assert course["url"] == "https://example.org/course"
    assert course["title"] == "Курс, общий" and course["captured_on"] == "2026-11-17"
    assert course["input_sha256"] == hashlib.sha256(page.encode()).hexdigest()
    assert course["snapshot_sha256"] == hashlib.sha256(snapshot).hexdigest()
    assert mirror["file"] == course["file"] and mirror["duplicate_content"] is True
    assert mirror["url"] == "https://example.org/mirror"
    assert (out / course["file"]).read_bytes() == snapshot
    assert (out / catalog["file"]).read_bytes() == pdf
    assert len(list(out.iterdir())) == 3
    repeated = run_snapshot_batch(tmp_path, rows)
    assert repeated.returncode == 2 and "already exists" in repeated.stderr


@pytest.mark.parametrize(("url", "captured_on", "filename", "error"), [
    ("https://example.org/course", "2026-11-17", "course.txt", "only once"),
    ("https://example.org/other", "2026-11-17", "../course.txt", "relative"),
    ("https://127.0.0.1/other", "2026-11-17", "course.txt", "invalid_source_url"),
    ("https://example.org/other", "20261117", "course.txt", "YYYY-MM-DD"),
    ("https://example.org/other", "2026-11-18", "missing.txt", "Expected a local file"),
])
def test_invalid_batch_leaves_no_output(tmp_path, url, captured_on, filename, error):
    (tmp_path / "course.txt").write_text("Local snapshot", encoding="utf-8")
    process = run_snapshot_batch(tmp_path, [
        ("First", "Courses", "https://example.org/course", "2026-11-17", "course.txt"),
        ("Second", "Courses", url, captured_on, filename),
    ])
    assert process.returncode == 2 and "Snapshot 2: " in process.stderr and error in process.stderr
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob("page-snapshots-*"))


def test_single_snapshot_command_keeps_existing_output(tmp_path):
    (tmp_path / "page.html").write_text("<main><p>Страница</p></main>", encoding="utf-8")
    command = [sys.executable, str(SNAPSHOT_SCRIPT), "page.html", "snapshot.txt"]
    subprocess.run(command, cwd=tmp_path, check=True)
    target = tmp_path / "snapshot.txt"
    assert target.read_bytes() == "Страница\n".encode()
    target.write_bytes(b"existing")
    repeated = subprocess.run(command, cwd=tmp_path, capture_output=True)
    assert repeated.returncode == 2 and target.read_bytes() == b"existing"


def load_stack():
    spec = importlib.util.spec_from_file_location("ci_stack", ROOT / "scripts/ci/stack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stack_resolves_repository_and_keeps_local_services_out_of_ci(tmp_path, monkeypatch):
    module = load_stack()
    assert (module.ROOT / "compose.yaml").is_file()
    (tmp_path / "compose.yaml").write_text((ROOT / "compose.yaml").read_text())
    target = tmp_path / ".ci-compose.yaml"
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "CONFIG", target)
    calls = []
    monkeypatch.setattr(module, "command", lambda *args: calls.append(args))
    monkeypatch.setattr(module, "compose", lambda *args: calls.append(args))
    module.prepare()
    config = yaml.safe_load(target.read_text())
    assert set(config["services"]) == module.SERVICES
    assert config["services"]["api"]["environment"]["PARTNER_API_REQUESTS_PER_MINUTE"] == "1000"
    assert "prometheus" not in config["services"]
    assert "phoenix" not in config["services"]
    frontend = config["services"]["caddy"]
    assert frontend["build"]["dockerfile"] == "infra/Dockerfile.frontend"
    assert not any(item.startswith("./") for item in frontend["volumes"])
    assert config["services"]["tests"]["environment"]["PARTNER_QDRANT_URL"]
    assert config["services"]["tests"]["command"][-1] == "not parser"
    assert calls[-1] == ("config", "--quiet")


@pytest.mark.parametrize(
    "event,ref,base,full,stack,release",
    [
        ("push", "refs/heads/feature/sample", "", "", False, False),
        ("push", "refs/heads/develop", "", "", True, False),
        ("push", "refs/heads/main", "", "", True, True),
        ("pull_request", "refs/pull/1/merge", "main", "", True, True),
        ("pull_request", "refs/pull/1/merge", "develop", "", True, False),
        ("workflow_dispatch", "refs/heads/develop", "", "true", True, True),
    ],
)
def test_acceptance_scope_matches_integration_and_release_events(
    tmp_path, event, ref, base, full, stack, release
):
    output = tmp_path / "scope"
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    environment = {
        **os.environ,
        "BASE_SHA": head,
        "BASE_BRANCH": base,
        "FULL_CHECKS": full,
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF": ref,
        "GITHUB_OUTPUT": str(output),
    }
    subprocess.run([sys.executable, "scripts/ci/scope.py"], cwd=ROOT, env=environment, check=True)
    assert output.read_text() == f"stack={str(stack).lower()}\nfull={str(release).lower()}\n"


@pytest.mark.skipif(sys.platform == "win32", reason="Linux container launcher")
@pytest.mark.parametrize("available", [1, 2, 8])
def test_converter_keeps_isolation_with_limited_host_cpus(tmp_path, available):
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1] == 'info':\n"
        "    print(os.environ['TEST_DOCKER_CPUS'])\n"
        "elif sys.argv[1] == 'run':\n"
        "    args = sys.argv[2:]\n"
        "    if float(args[args.index('--cpus') + 1]) > int(os.environ['TEST_DOCKER_CPUS']):\n"
        "        raise SystemExit(125)\n"
        "    Path(os.environ['TEST_DOCKER_ARGUMENTS']).write_text(json.dumps(args))\n"
    )
    docker.chmod(0o700)
    source = tmp_path / "source.docx"
    source.write_bytes(b"synthetic")
    output = tmp_path / "output"
    output.mkdir()
    arguments = tmp_path / "arguments.json"
    subprocess.run(
        ["bash", str(ROOT / "scripts/run-parser.sh"), str(source), str(output)],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "TEST_DOCKER_CPUS": str(available),
            "TEST_DOCKER_ARGUMENTS": str(arguments),
        },
        check=True,
    )
    args = json.loads(arguments.read_text())
    assert args[args.index("--cpus") + 1] == str(min(4, available))
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert "--read-only" in args
