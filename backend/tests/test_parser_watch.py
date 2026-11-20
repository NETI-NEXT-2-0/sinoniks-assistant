"""Manual watch must recover from poll errors and remain cancellable."""

import asyncio
import logging

import pytest

from partner_agent.documents import parser_host


async def test_watch_retries_without_logging_exception_details(monkeypatch, caplog):
    calls = []
    delays = []

    async def poll(lock_fd):
        calls.append(lock_fd)
        if len(calls) == 1:
            raise RuntimeError("synthetic private source content")
        return 1, 0

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(parser_host, "run_once", poll)
    monkeypatch.setattr(parser_host.asyncio, "sleep", sleep)
    with caplog.at_level(logging.INFO), pytest.raises(asyncio.CancelledError):
        await parser_host.watch(42, 3)
    assert calls == [42, 42] and delays == [3, 3]
    assert "parser_host_poll_unavailable" in caplog.text
    assert "synthetic private source content" not in caplog.text
    assert "parser_host_poll processed=1 retries=0" in caplog.text


async def test_watch_rejects_out_of_range_interval():
    for interval in (0, 3601):
        with pytest.raises(ValueError, match="invalid_parser_watch_interval"):
            await parser_host.watch(42, interval)


def test_docker_unavailable_is_sanitized_and_blocks_host(monkeypatch):
    import subprocess

    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "docker", output="synthetic secret")

    monkeypatch.setattr(parser_host.subprocess, "check_output", unavailable)
    with pytest.raises(RuntimeError, match="^parser_infrastructure_unavailable$"):
        parser_host.assert_no_parser_containers()


def test_targeted_blocked_job_exits_without_claiming_retry(tmp_path, monkeypatch, caplog):
    import os
    import uuid

    async def blocked(version_id, lock_fd):
        return "blocked"

    monkeypatch.setattr(
        parser_host, "acquire_host_lock", lambda: os.open(tmp_path / "lock", os.O_CREAT)
    )
    monkeypatch.setattr(parser_host, "try_process_version", blocked)
    monkeypatch.setattr("sys.argv", ["parser_host", "--version", str(uuid.uuid4())])
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as result:
        parser_host.main()
    assert result.value.code == 1
    assert "parser_host_completed" not in caplog.text
