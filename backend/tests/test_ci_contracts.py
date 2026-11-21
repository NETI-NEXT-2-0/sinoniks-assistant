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
        ("Курс, общий", "Программы", "https://EXAMPLE.org/course", "2026-11-21", "страница.html"),
        ("Зеркало", "Программы", "https://example.org/mirror", "2026-11-21", "duplicate.txt"),
        ("Каталог", "Документы", "https://example.org/catalog", "2026-11-20", "catalog.pdf"),
    ]
    process = run_snapshot_batch(tmp_path, rows)
    assert process.returncode == 0, process.stderr
    out = tmp_path / "out"
    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["snapshot_count"] == 3 and inventory["unique_file_count"] == 2
    assert inventory["snapshots_by_topic"] == {"Программы": 2, "Документы": 1}
    course, mirror, catalog = inventory["snapshots"]
    assert course["url"] == "https://example.org/course"
    assert course["title"] == "Курс, общий" and course["captured_on"] == "2026-11-21"
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
    ("https://example.org/course", "2026-11-21", "course.txt", "only once"),
    ("https://example.org/other", "2026-11-21", "../course.txt", "relative"),
    ("https://127.0.0.1/other", "2026-11-21", "course.txt", "invalid_source_url"),
    ("https://example.org/other", "20261121", "course.txt", "YYYY-MM-DD"),
    ("https://example.org/other", "2026-11-22", "missing.txt", "Expected a local file"),
])
def test_invalid_batch_leaves_no_output(tmp_path, url, captured_on, filename, error):
    (tmp_path / "course.txt").write_text("Local snapshot", encoding="utf-8")
    process = run_snapshot_batch(tmp_path, [
        ("First", "Courses", "https://example.org/course", "2026-11-21", "course.txt"),
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


def test_heavy_checks_and_release_status_are_reserved_for_main_pull_requests():
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"pull_request"}
    pull_request = workflow["on"]["pull_request"]
    assert set(pull_request["branches"]) == {"develop", "main"}
    assert set(pull_request["types"]) == {"opened", "synchronize", "reopened"}
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert set(jobs) == {"quality", "postgres", "security", "containers", "required"}
    assert set(jobs["required"]["needs"]) == set(jobs) - {"required"}
    condition = "${{ github.base_ref == 'main' }}"
    assert jobs["containers"]["if"] == jobs["postgres"]["if"] == condition
    assert jobs["required"]["name"] == (
        "${{ github.base_ref == 'main' && 'release-required' || 'required' }}"
    )
    gate = jobs["required"]["steps"][0]
    assert gate["env"]["RELEASE_PR"] == condition
    assert 'test "$STACK" = success' in gate["run"]
    assert 'test "$STACK" = skipped' in gate["run"]
    assert 'test "$POSTGRES" = success' in gate["run"]
    assert 'test "$POSTGRES" = skipped' in gate["run"]
    acceptance_steps = {step.get("name") for step in jobs["containers"]["steps"]}
    assert {
        "Browser acceptance", "Recovery acceptance", "Isolated conversion acceptance",
    } <= acceptance_steps

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
