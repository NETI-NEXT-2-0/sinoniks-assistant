import hashlib
import json
from pathlib import Path

import pytest

from partner_agent.knowledge19 import (
    FILES,
    QUESTIONS,
    PreparedSource,
    diagnose,
    load_checks,
    load_sources,
    main,
    prepare,
)


def source_dir(tmp_path: Path, *, raw: bytes | None = None) -> Path:
    directory = tmp_path / "input"
    directory.mkdir()
    for name in FILES:
        (directory / name).write_bytes(raw or b"# Fixture\n\n## Section\nSafe synthetic text.\n")
    return directory


def test_exact_sources_and_preview_locators_are_not_publication(tmp_path: Path) -> None:
    raw = "\ufeff# Тест\r\n\r\n## Раздел\r\nСинтетический текст.\r\n".encode()
    directory = source_dir(tmp_path, raw=raw)
    output = tmp_path / "output"
    report = prepare(directory, output)
    assert report["preview_only"] and not report["published"]
    assert not report["retrieval"]["published_access_verified"]
    assert not report["retrieval"]["model_answers_evaluated"]
    assert json.loads((output / "manifest.json").read_bytes()) == report
    for document in report["documents"]:
        assert (output / document["file"]).read_bytes() == raw
        assert (directory / document["file"]).read_bytes() == raw
        assert document["source_sha256"] == hashlib.sha256(raw).hexdigest()
        assert document["media_type"] == "text/markdown"
        decoded = raw.decode()
        for locator in document["locators"]:
            assert locator["reviewed_copy"]
            assert "page" not in locator
            assert decoded[locator["start"] : locator["end"]]
        assert any(loc.get("section") == "Раздел" for loc in document["locators"])


def test_prepare_never_overwrites_existing_directory(tmp_path: Path) -> None:
    directory = source_dir(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "do-not-touch"
    sentinel.write_bytes(b"important")
    assert main([str(directory), str(output)]) == 2
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"important"


@pytest.mark.parametrize("raw", [b"\xff", b"# Title\n\x00bad", b"", b"No headings here"])
def test_invalid_source_leaves_no_output(tmp_path: Path, raw: bytes) -> None:
    directory = source_dir(tmp_path)
    (directory / FILES[-1]).write_bytes(raw)
    output = tmp_path / "output"
    assert main([str(directory), str(output)]) == 2
    assert not output.exists()


def test_missing_source_does_not_silently_use_old_guides(tmp_path: Path) -> None:
    directory = source_dir(tmp_path)
    (directory / FILES[1]).unlink()
    (directory / "administrator.txt").write_text("old guide")
    assert main([str(directory), str(tmp_path / "output")]) == 2


def test_size_bound_precedes_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from partner_agent import knowledge19

    directory = source_dir(tmp_path)
    monkeypatch.setattr(knowledge19, "MAX_SOURCE_BYTES", 8)
    with pytest.raises(ValueError, match="size limit"):
        load_sources(directory)


def test_diagnosis_is_conservative_about_ties() -> None:
    source = PreparedSource(
        FILES[0],
        b"",
        "unused",
        [("needle", {"section": "target"})] + [("needle", {"section": "other"}) for _ in range(8)],
    )
    questions = (("Q1", "needle", FILES[0], "target"),)
    report = diagnose([source], questions=questions)
    assert report["questions"][0]["best_rank"] == 1
    assert report["questions"][0]["worst_rank"] == 9
    assert report["guaranteed"] == 0
    assert diagnose([source], limit=9, questions=questions)["guaranteed"] == 1
    with pytest.raises(ValueError):
        diagnose([source], limit=0)


def test_fenced_fake_heading_does_not_create_citation_section(tmp_path: Path) -> None:
    directory = source_dir(tmp_path, raw=b"# Real\n```\n## Fake\n```\n## Actual\nbody\n")
    sources = load_sources(directory)
    sections = {locator.get("section") for _, locator in sources[0].units}
    assert "Fake" not in sections
    assert "Actual" in sections


def test_lexical_misses_still_produce_reviewable_bundle(tmp_path: Path) -> None:
    directory = source_dir(tmp_path)
    output = tmp_path / "output"
    assert main([str(directory), str(output)]) == 1
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert manifest["retrieval"]["guaranteed"] == 0
    assert manifest["retrieval"]["total"] == 12
    # No excerpt/answer is emitted into the diagnostic metadata.
    assert "Safe synthetic text" not in (output / "manifest.json").read_text()


def test_separate_checks_do_not_replace_fixed_originals(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    original = QUESTIONS
    row = {"id": "H1", "query": "needle", "file": FILES[0], "section": "target"}
    path.write_text(json.dumps({"questions": [row]}))
    assert load_checks(path) == (("H1", "needle", FILES[0], "target"),)
    assert QUESTIONS == original and len(QUESTIONS) == 12
    path.write_text(json.dumps({"questions": [row, row]}))
    with pytest.raises(ValueError, match="Duplicate"):
        load_checks(path)


@pytest.mark.parametrize("change", [{"file": "../../private"}, {"query": ""}, {"section": None}])
def test_invalid_checks_are_rejected_before_preparation(tmp_path: Path, change: dict) -> None:
    row = {"id": "H1", "query": "needle", "file": FILES[0], "section": "target", **change}
    path = tmp_path / "checks.json"
    path.write_text(json.dumps({"questions": [row]}))
    with pytest.raises(ValueError):
        load_checks(path)
