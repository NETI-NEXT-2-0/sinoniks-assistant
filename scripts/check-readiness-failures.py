#!/usr/bin/env python3
"""Pause local PG/Garage in turn, prove live/ready separation, always resume.

Run only on this disposable development stack, after all migrations/test jobs
finish. Does not delete data. The two named containers resume in finally even
after Ctrl-C/SIGTERM. Do not run concurrently with other integration checks.
"""

import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ORIGIN = "http://localhost:8080"
ROOT = Path(__file__).resolve().parents[1]


def interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def request(path: str) -> httpx.Response:
    return httpx.get(ORIGIN + path, timeout=25, trust_env=False)


def main() -> None:
    signal.signal(signal.SIGTERM, interrupt)
    assert request("/health/ready").status_code == 200
    for service in ("postgres", "garage"):
        subprocess.run(["docker", "compose", "pause", service], cwd=ROOT, check=True)
        try:
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=1) as pool:
                waiting = pool.submit(request, "/health/ready")
                live = request("/health/live")
                assert live.status_code == 200
                assert time.monotonic() - start < 2, "Liveness waited on a dependency"
                response = waiting.result(timeout=25)
            elapsed = time.monotonic() - start
            assert response.status_code == 503, (service, response.status_code)
            assert response.json()["code"] in {
                "dependency_timeout",
                "dependency_unavailable",
                "storage_unavailable",
            }
            assert elapsed < 22, "Dependency failure did not return within cleanup budget"
            print(f"PASS: paused {service}, live=200, ready=503 in {elapsed:.2f}s")
        finally:
            subprocess.run(["docker", "compose", "unpause", service], cwd=ROOT, check=True)
        for _ in range(20):
            if request("/health/ready").status_code == 200:
                break
            time.sleep(0.5)
        else:
            raise AssertionError(f"Readiness did not recover after resuming {service}")
    print("PASS: dependencies resumed, readiness recovered; no data deleted")


if __name__ == "__main__":
    main()
