"""Host parser recovery must not delete unrelated paths or run beside a parser."""

import os

import pytest

from partner_agent.documents import parser_host


def test_cleanup_removes_only_private_job_directories(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(parser_host, "TEMP_ROOT", root)
    monkeypatch.setattr(parser_host.subprocess, "check_output", lambda *a, **k: b"")
    stale = root / "job-12345678"
    stale.mkdir()
    (stale / "source.pdf").write_bytes(b"synthetic")
    unknown = root / "unrelated"
    unknown.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    (stale / "linked-outside").symlink_to(outside, target_is_directory=True)
    (root / "job-abcdefgh").symlink_to(outside, target_is_directory=True)
    assert parser_host.cleanup_temporary_jobs() == 1
    assert not stale.exists()
    assert unknown.exists() and (outside / "keep").read_bytes() == b"keep"
    assert (root / "job-abcdefgh").is_symlink()


def test_cleanup_refuses_active_container_or_insecure_root(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(parser_host, "TEMP_ROOT", root)
    job = root / "job-12345678"
    job.mkdir()
    monkeypatch.setattr(parser_host.subprocess, "check_output", lambda *a, **k: b"container-id")
    with pytest.raises(RuntimeError, match="parser_container_present"):
        parser_host.cleanup_temporary_jobs()
    assert job.exists()
    monkeypatch.setattr(parser_host.subprocess, "check_output", lambda *a, **k: b"")
    os.chmod(root, 0o755)
    with pytest.raises(RuntimeError, match="invalid_parser_temp_root"):
        parser_host.cleanup_temporary_jobs()
    assert job.exists()
