"""Private, offline preparation of the three priority 1.9 Markdown sources."""

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from partner_agent.documents.canonical import extract_utf8
from partner_agent.documents.edited_copy import MARKDOWN_LOCATORS, ReviewedCopy, render_copy
from partner_agent.infra.errors import APIError
from partner_agent.retrieval.ranking import PROFILE, rank_scores

FILES = (
    "01_scope_functions_synonyx_1.9.md",
    "02_methodology.md",
    "03_glossary.md",
)
MAX_SOURCE_BYTES = 1_000_000

# Source/section checks, not answer examples or a replacement for model evaluation.
QUESTIONS = (
    ("K19-01", "Сколько правил поддерживает один ПАК?", FILES[0], "9."),
    ("K19-02", "Есть ли встроенный антивирус?", FILES[0], "9."),
    ("K19-03", "Какие проверки файлов выполняются через ICAP?", FILES[0], "Проверки файлов"),
    ("K19-04", "Какие протоколы поддерживаются для PULL и PUSH?", FILES[0], "Способы передачи"),
    ("K19-05", "Как остановить обмен ключом?", FILES[0], "6."),
    ("K19-06", "Какие журналы и оповещения доступны?", FILES[0], "7."),
    ("K19-07", "Чем Синоникс отличается от NGFW?", FILES[0], "10."),
    ("K19-08", "Где разместить Синоникс и МЭ относительно закрытой сети?", FILES[1], "2."),
    ("K19-09", "Как считать правила передачи данных с общим получателем?", FILES[1], "3.1."),
    ("K19-10", "Как считать правила передачи файлов PULL PUSH?", FILES[1], "3.2."),
    ("K19-11", "Как подсчитать пользователей файловой передачи?", FILES[1], "7."),
    ("K19-12", "Что означает DLP?", FILES[2], "Передача файлов"),
)


@dataclass(frozen=True)
class PreparedSource:
    name: str
    raw: bytes
    digest: str
    units: list[tuple[str, dict[str, Any]]]


def load_sources(directory: Path) -> list[PreparedSource]:
    result = []
    for name in FILES:
        with (directory / name).open("rb") as handle:
            raw = handle.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("Source exceeds size limit")
        digest = hashlib.sha256(raw).hexdigest()
        # Preview UUIDs are deterministic and explicitly not database identities.
        document_id = uuid5(NAMESPACE_URL, "sinoniks-knowledge19-preview:" + name)
        version_id = uuid5(document_id, digest)
        canonical = extract_utf8(raw, document_id, version_id, "text/markdown")
        copy = ReviewedCopy(
            document_id=document_id,
            version_id=version_id,
            canonical_sha256=hashlib.sha256(canonical.model_dump_json().encode()).hexdigest(),
            text=raw.decode("utf-8"),
            locator_profile=MARKDOWN_LOCATORS,
        )
        copy_digest = hashlib.sha256(copy.model_dump_json().encode()).hexdigest()
        units = [(text, locator) for _, text, _, locator in render_copy(copy, copy_digest)]
        if not any(locator.get("section") for _, locator in units):
            raise ValueError("Expected Markdown sections")
        result.append(PreparedSource(name, raw, digest, units))
    return result


def diagnose(
    sources: list[PreparedSource],
    *,
    limit: int = 8,
    questions: tuple[tuple[str, str, str, str], ...] = QUESTIONS,
) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise ValueError("Invalid retrieval limit")
    units = [(source.name, text, locator) for source in sources for text, locator in source.units]
    results = []
    for identifier, query, expected_file, section in questions:
        scores = rank_scores(query, [text for _, text, _ in units])
        anchors = [
            index
            for index, (name, _, locator) in enumerate(units)
            if name == expected_file and str(locator.get("section", "")).startswith(section)
        ]
        best = max((scores[index] for index in anchors), default=0)
        first = 1 + sum(score > best for score in scores) if best else None
        last = (
            first + scores.count(best) - sum(scores[index] == best for index in anchors)
            if first is not None
            else None
        )
        results.append(
            {
                "id": identifier,
                "expected_file": expected_file,
                "expected_section_prefix": section,
                "matching_units": len(anchors),
                "best_rank": first,
                "worst_rank": last,
                "guaranteed_in_top_k": last is not None and last <= limit,
            }
        )
    return {
        "profile": PROFILE,
        "top_k": limit,
        "questions": results,
        "guaranteed": sum(row["guaranteed_in_top_k"] for row in results),
        "total": len(results),
        "model_answers_evaluated": False,
        "published_access_verified": False,
    }


def prepare(
    directory: Path,
    output: Path,
    *,
    questions: tuple[tuple[str, str, str, str], ...] = QUESTIONS,
) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Output directory already exists")
    sources = load_sources(directory)
    manifest = {
        "schema_version": 1,
        "product_version": "1.9",
        "preview_only": True,
        "published": False,
        "documents": [
            {
                "file": source.name,
                "source_sha256": source.digest,
                "bytes": len(source.raw),
                "media_type": "text/markdown",
                "locator_profile": MARKDOWN_LOCATORS,
                "units": len(source.units),
                "locators": [locator for _, locator in source.units],
            }
            for source in sources
        ],
        "retrieval": diagnose(sources, questions=questions),
    }
    # Validate all input before creating a new directory. No source is rewritten.
    output.mkdir()
    for source in sources:
        with (output / source.name).open("xb") as handle:
            handle.write(source.raw)
    with (output / "manifest.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return manifest


def load_checks(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    with path.open("rb") as handle:
        raw = handle.read(100_001)
    if len(raw) > 100_000:
        raise ValueError("Checks exceed size limit")
    data = json.loads(raw)
    rows = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("Expected bounded questions array")
    checks: list[tuple[str, str, str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "query", "file", "section"}:
            raise ValueError("Invalid check keys")
        if any(not isinstance(value, str) or not value.strip() for value in row.values()):
            raise ValueError("Invalid check value")
        if len(row["query"].encode()) > 32768 or row["file"] not in FILES:
            raise ValueError("Invalid check bounds")
        checks.append((row["id"], row["query"], row["file"], row["section"]))
    if len({row[0] for row in checks}) != len(checks):
        raise ValueError("Duplicate check IDs")
    return tuple(checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--checks", type=Path, help="Separate fixed formulations; default checks unchanged"
    )
    args = parser.parse_args(argv)
    try:
        checks = load_checks(args.checks) if args.checks else QUESTIONS
        manifest = prepare(args.source, args.output, questions=checks)
    except (OSError, ValueError, APIError):
        print(
            "Knowledge preparation failed: check sources and new output directory.", file=sys.stderr
        )
        return 2
    retrieval = manifest["retrieval"]
    print(
        "Prepared 3 private sources; lexical anchors "
        f"{retrieval['guaranteed']}/{retrieval['total']}."
    )
    return 0 if retrieval["guaranteed"] == retrieval["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
