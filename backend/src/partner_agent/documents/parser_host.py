"""Host-only parser coordinator. Never run in the API/worker containers.

One host lock is inherited by the sandbox launcher. Pending rows are durable;
no lease or automatic takeover is claimed. Run again explicitly after failures.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, or_, select
from starlette.concurrency import run_in_threadpool

from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.documents.docling_adapter import ParsedDocument, normalize_docling
from partner_agent.documents.ingest import DOCLING_MEDIA_TYPES, storage
from partner_agent.documents.service import lock_document
from partner_agent.infra.db import session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_chat_files import ChatFile
from partner_agent.infra.models_documents import Document, DocumentVersion
from partner_agent.infra.storage import MAX_OBJECT_BYTES, object_key

ROOT = Path(__file__).resolve().parents[4]
LOGGER = logging.getLogger("partner_agent.parser_host")
TEMP_ROOT = Path(tempfile.gettempdir()) / f"partner-agent-parser-work-{os.getuid()}"
JOB_DIRECTORY = re.compile(r"job-[a-z0-9_]{8}\Z")


def acquire_host_lock() -> int:
    """Keep one host parser invocation, even if the parent is killed."""
    path = os.environ.get("PARTNER_PARSER_LOCK_PATH", "/tmp/partner-agent-parser.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError("invalid_parser_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def assert_no_parser_containers() -> None:
    # An orphan from an interrupted launcher must be resolved manually.
    try:
        containers = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", "label=partner-agent.parser=1"],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("parser_infrastructure_unavailable") from None
    if containers.strip():
        raise RuntimeError("parser_container_present")


def private_temporary_root() -> Path:
    """Reject existing symlinks, foreign ownership and accessible job roots."""
    try:
        TEMP_ROOT.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = TEMP_ROOT.lstat()
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("invalid_parser_temp_root")
    return TEMP_ROOT


def cleanup_temporary_jobs() -> int:
    """Called only under host lock; never touch unknown names or legacy /tmp paths."""
    assert_no_parser_containers()
    root = private_temporary_root()
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("unsafe_parser_cleanup_platform")
    cleaned = 0
    for entry in root.iterdir():
        if not JOB_DIRECTORY.fullmatch(entry.name):
            continue
        info = entry.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            continue
        shutil.rmtree(entry)
        cleaned += 1
    return cleaned


def run_isolated(source: bytes, suffix: str, lock_fd: int) -> dict[str, object]:
    """Never expose source or parser diagnostics in application logs."""
    assert_no_parser_containers()
    root = private_temporary_root()
    with tempfile.TemporaryDirectory(prefix="job-", dir=root) as temporary:
        directory = Path(temporary)
        input_path = directory / ("source" + suffix)
        input_path.write_bytes(source)
        output = directory / "output"
        output.mkdir()
        completed = subprocess.run(
            [str(ROOT / "scripts/run-parser.sh"), str(input_path), str(output)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=640,
            pass_fds=(lock_fd,),
        )
        if completed.returncode != 0:
            raise RuntimeError("parser_timeout" if completed.returncode == 124 else "parser_failed")
        output_fd = os.open(output / "canonical-docling.json", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(output_fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_OBJECT_BYTES:
                raise RuntimeError("parser_output_size")
            with os.fdopen(output_fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_OBJECT_BYTES + 1)
            if len(data) > MAX_OBJECT_BYTES:
                raise RuntimeError("parser_output_size")
        finally:
            os.close(output_fd)
        result = json.loads(data)
        if not isinstance(result, dict):
            raise RuntimeError("parser_output_invalid")
        return result


async def process_version(version_id: UUID, lock_fd: int) -> bool:
    """Download and normalize outside row locks, then recheck all metadata."""
    async with session_factory()() as db:
        version = await db.get(DocumentVersion, version_id)
        if (
            version is None or version.status != "pending" or version.canonical_key is not None
            or version.parser_attempts >= 5
        ):
            return False
        if (
            version.pipeline_version != "docling-adapter-v1"
            or version.media_type not in DOCLING_MEDIA_TYPES
        ):
            return False
        document = await db.get(Document, version.document_id)
        if document is None:
            return False
        actor = await db.get(User, document.owner_id)
        if (
            actor is None or actor.disabled or actor.role not in MANAGER_ROLES
            or document.deleted_at is not None or document.scope != "knowledge"
        ):
            return False
        document = await lock_document(db, document.id, actor)
        if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
            raise APIError(403, "curator_required", "Curator permission required")
        document_id = document.id
        key, expected_hash, size = version.original_key, version.source_sha256, version.size_bytes
        media_type = version.media_type
        await db.commit()

        def convert() -> tuple[str, str]:
            adapter = storage()
            try:
                source = adapter.get(adapter.originals_bucket, key).data
                if len(source) != size or hashlib.sha256(source).hexdigest() != expected_hash:
                    raise RuntimeError("source_checksum_mismatch")
                parsed = run_isolated(source, DOCLING_MEDIA_TYPES[media_type], lock_fd)
                parser_version = parsed.get("partner_parser_version")
                if parser_version != "docling-2.150.0":
                    raise RuntimeError("parser_version_mismatch")
                canonical = normalize_docling(
                    parsed, document_id, version_id, expected_hash, parser_version
                )
                data = canonical.model_dump_json().encode()
                digest = hashlib.sha256(data).hexdigest()
                canonical_key = object_key(document_id, version_id, f"canonical-{digest}.json")
                adapter.put(adapter.derived_bucket, canonical_key, data, "application/json")
                return canonical_key, digest
            finally:
                adapter.client.close()

        canonical_key, digest = await run_in_threadpool(convert)
        await lock_document(db, document_id, actor)
        current = await db.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.id == version_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is None or (
            current.document_id != document_id
            or current.status != "pending"
            or current.canonical_key is not None
            or current.pipeline_version != "docling-adapter-v1"
            or current.original_key != key
            or current.source_sha256 != expected_hash
            or current.size_bytes != size
            or current.media_type != media_type
            or actor.role not in MANAGER_ROLES
        ):
            raise APIError(409, "ingest_state_changed", "Version changed during parsing")
        current.canonical_key = canonical_key
        current.canonical_sha256 = digest
        current.error_code = None
        current.parser_retry_at = None
        await db.commit()
        return True


RETRY_CODES = frozenset(
    {
        "parser_timeout", "parser_failed", "parser_version_mismatch",
        "parser_output_size", "parser_output_invalid", "parser_container_present",
        "source_checksum_mismatch",
    }
)


async def try_process_version(version_id: UUID, lock_fd: int) -> str:
    async with session_factory()() as db:
        version = await db.get(DocumentVersion, version_id)
        if (
            version is None or version.status != "pending" or version.canonical_key is not None
            or version.parser_attempts >= 5 or version.pipeline_version != "docling-adapter-v1"
        ):
            return "skipped"
        attempt_count, retry_at, error_code = (
            version.parser_attempts, version.parser_retry_at, version.error_code
        )
    try:
        return "processed" if await process_version(version_id, lock_fd) else "skipped"
    except Exception as error:
        if isinstance(error, RuntimeError) and str(error) in {
            "parser_container_present", "parser_infrastructure_unavailable"
        }:
            LOGGER.warning("parser_host_blocked code=%s", str(error))
            return "blocked"
        if isinstance(error, APIError) and error.code in {
            "access_revoked", "not_found", "curator_required", "ingest_state_changed"
        }:
            return "skipped"
        code = (
            str(error) if isinstance(error, RuntimeError) and str(error) in RETRY_CODES
            else "parser_retry_required"
        )
        if isinstance(error, subprocess.TimeoutExpired):
            code = "parser_timeout"
        recorded = False
        try:
            async with session_factory()() as db:
                version = await db.scalar(
                    select(DocumentVersion)
                    .where(DocumentVersion.id == version_id)
                    .with_for_update()
                )
                if (
                    version is not None and version.status == "pending"
                    and version.canonical_key is None
                    and version.pipeline_version == "docling-adapter-v1"
                    and version.parser_attempts == attempt_count
                    and version.parser_retry_at == retry_at and version.error_code == error_code
                ):
                    version.error_code = code
                    version.parser_attempts = min(version.parser_attempts + 1, 5)
                    if version.parser_attempts >= 5:
                        version.status = "failed"
                        version.parser_retry_at = None
                    else:
                        now = await db.scalar(select(func.now()))
                        assert now is not None
                        version.parser_retry_at = now + timedelta(
                            seconds=30 * 2 ** (version.parser_attempts - 1)
                        )
                    await db.commit()
                    recorded = True
        except Exception:
            # Database unavailability must not leak connection details in diagnostics.
            LOGGER.warning("parser_failure_state_unavailable version_id=%s", version_id)
        if recorded:
            LOGGER.warning("parser_retry_required version_id=%s code=%s", version_id, code)
            return "retry"
        LOGGER.info("parser_attempt_not_recorded version_id=%s", version_id)
        return "skipped"


async def run_once(lock_fd: int) -> tuple[int, int]:
    from partner_agent.documents.chat_files import pending_files, process_file

    private_candidates = await pending_files(binary=True)
    if private_candidates:
        await run_in_threadpool(assert_no_parser_containers)
    def parse_private(source: bytes, item: ChatFile) -> ParsedDocument:
        parsed = run_isolated(source, DOCLING_MEDIA_TYPES[item.media_type], lock_fd)
        parser_version = parsed.get("partner_parser_version")
        if parser_version != "docling-2.150.0":
            raise RuntimeError("parser_version_mismatch")
        return normalize_docling(parsed, item.chat_id, item.id, item.source_sha256, parser_version)

    private_processed = 0
    for identifier in private_candidates:
        private_processed += int(await process_file(identifier, parse_private))
    async with session_factory()() as db:
        candidates = list(
            (
                await db.scalars(
                    select(DocumentVersion.id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .join(User, User.id == Document.owner_id)
                    .where(
                        DocumentVersion.status == "pending",
                        DocumentVersion.canonical_key.is_(None),
                        DocumentVersion.parser_attempts < 5,
                        or_(DocumentVersion.parser_retry_at.is_(None),
                            DocumentVersion.parser_retry_at <= func.now()),
                        DocumentVersion.pipeline_version == "docling-adapter-v1",
                        DocumentVersion.media_type.in_(DOCLING_MEDIA_TYPES),
                        Document.deleted_at.is_(None),
                        Document.scope == "knowledge",
                        User.disabled.is_(False),
                        User.role.in_(MANAGER_ROLES),
                    )
                    .order_by(DocumentVersion.created_at)
                    .limit(100)
                )
            ).all()
        )
    processed, failures = private_processed, 0
    if candidates:
        await run_in_threadpool(assert_no_parser_containers)
    for version_id in candidates:
        result = await try_process_version(version_id, lock_fd)
        if result == "blocked":
            raise RuntimeError("parser_infrastructure_unavailable")
        processed += int(result == "processed")
        failures += int(result == "retry")
    return processed, failures


async def watch(lock_fd: int, interval_seconds: int) -> None:
    """Poll under the host lock until explicitly stopped; no automatic takeover."""
    if not 1 <= interval_seconds <= 3600:
        raise ValueError("invalid_parser_watch_interval")
    while True:
        try:
            processed, failures = await run_once(lock_fd)
            if processed or failures:
                LOGGER.info("parser_host_poll processed=%s retries=%s", processed, failures)
        except Exception:
            # Never log source text, credentials, or exception messages.
            LOGGER.warning("parser_host_poll_unavailable")
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated document parsing on the host")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--version", type=UUID, help="Process one specific pending version")
    action.add_argument(
        "--cleanup-temporary", action="store_true", help="Remove recognized orphan job files"
    )
    action.add_argument("--watch", action="store_true", help="Continuously poll due parser jobs")
    parser.add_argument(
        "--interval", type=int, default=10, help="Poll interval in seconds (1–3600)"
    )
    args = parser.parse_args()
    if not 1 <= args.interval <= 3600:
        parser.error("--interval must be between 1 and 3600 seconds")
    if args.interval != 10 and not args.watch:
        parser.error("--interval requires --watch")
    logging.basicConfig(level=logging.INFO)
    try:
        lock_fd = acquire_host_lock()
    except (BlockingIOError, RuntimeError, OSError):
        LOGGER.error("parser_host_lock_unavailable")
        raise SystemExit(1) from None
    try:
        if args.cleanup_temporary:
            LOGGER.info("parser_temporary_jobs_cleaned count=%s", cleanup_temporary_jobs())
            return
        if args.watch:
            asyncio.run(watch(lock_fd, args.interval))
            return
        if args.version is None:
            count, failures = asyncio.run(run_once(lock_fd))
        else:
            result = asyncio.run(try_process_version(args.version, lock_fd))
            if result == "blocked":
                raise SystemExit(1)
            count, failures = int(result == "processed"), int(result == "retry")
        LOGGER.info("parser_host_completed processed=%s retries=%s", count, failures)
        if failures:
            raise SystemExit(1)
    except Exception:
        LOGGER.error("parser_host_unavailable")
        raise SystemExit(1) from None
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
