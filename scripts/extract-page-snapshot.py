#!/usr/bin/env python3
"""Extract text from a saved HTML page or prepare a local bundle of snapshots."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import date
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 25 * 1024 * 1024
MANIFEST_COLUMNS = ("title", "topic", "url", "captured_on", "file")
OMIT = frozenset(
    {"head", "script", "style", "template", "noscript", "iframe", "svg", "nav", "footer", "form"}
)
VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
     "track", "wbr"}
)
BREAK = frozenset(
    {"article", "aside", "blockquote", "br", "div", "dl", "dt", "dd", "h1", "h2", "h3", "h4",
     "h5", "h6", "li", "main", "p", "pre", "section", "table", "tr"}
)


class Snapshot(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden: list[str] = []
        self.parts: list[str] = []
        self.cells = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if self.hidden or tag in OMIT or "hidden" in attributes:
            if tag not in VOID:
                self.hidden.append(tag)
            return
        if tag in BREAK:
            self.parts.append("\n")
        if tag == "tr":
            self.cells = 0
        if tag in {"td", "th"}:
            if self.cells:
                self.parts.append(" | ")
            self.cells += 1

    def handle_endtag(self, tag):
        if self.hidden:
            # A matching close also ends unclosed optional child tags.
            if tag in self.hidden:
                while self.hidden.pop() != tag:
                    pass
        elif tag in BREAK and tag not in VOID:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data.replace("\r", " ").replace("\n", " "))

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def read_bounded(path: Path, maximum: int) -> bytes:
    if not path.is_file():
        raise ValueError("Expected a local file")
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError(f"Input exceeds the {maximum} byte limit")
    return raw


def extract_html(raw: bytes) -> bytes:
    snapshot = Snapshot()
    try:
        snapshot.feed(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise ValueError(
            "Save the page as UTF-8 HTML or copy its text into a UTF-8 TXT file"
        ) from None
    snapshot.close()
    result = snapshot.text()
    if not result or "\x00" in result:
        raise ValueError("No page text found. Save or copy the rendered text manually.")
    return (result + "\n").encode()


def prepare_file(source: Path) -> tuple[bytes, bytes, str]:
    extension = source.suffix.lower()
    if extension in {".html", ".htm"}:
        raw = read_bounded(source, MAX_HTML_BYTES)
        return raw, extract_html(raw), ".txt"
    if extension not in {".txt", ".md", ".pdf"}:
        raise ValueError("Expected HTML, TXT, Markdown or PDF")
    raw = read_bounded(source, MAX_SNAPSHOT_BYTES)
    if extension == ".pdf":
        if not raw.startswith(b"%PDF-"):
            raise ValueError("File does not have a PDF header")
    elif b"\x00" in raw or not raw.decode("utf-8-sig").strip():
        raise ValueError("Expected nonempty UTF-8 text")
    return raw, raw, extension


def prepare_batch(manifest: Path, output: Path) -> None:
    from partner_agent.documents.web_source import normalize_source_url

    base = manifest.resolve().parent
    if output.exists():
        raise ValueError("Output directory already exists")
    records = read_bounded(manifest, 256 * 1024).decode("utf-8-sig")
    rows = csv.DictReader(StringIO(records, newline=""), strict=True)
    columns = rows.fieldnames or []
    if set(columns) != set(MANIFEST_COLUMNS) or len(columns) != len(MANIFEST_COLUMNS):
        raise ValueError("CSV header must contain title,topic,url,captured_on,file")

    # Stage the complete bundle before making it visible at the requested path.
    with TemporaryDirectory(prefix="page-snapshots-", dir=output.parent) as temp:
        bundle = Path(temp) / "bundle"
        bundle.mkdir()
        snapshots = []
        identities = set()
        unique_files: dict[tuple[str, str], str] = {}
        topics: Counter[str] = Counter()
        for number, row in enumerate(rows, start=1):
            try:
                if number > 200:
                    raise ValueError("Split manifests into batches of up to 200 snapshots")
                fields = [(row[column] or "").strip() for column in MANIFEST_COLUMNS]
                if None in row or not all(fields):
                    raise ValueError("Each row must contain all five fields")
                title, topic, url, captured, name = fields
                if not title.isprintable() or not topic.isprintable():
                    raise ValueError("Title and topic must be on a single line")
                if len(title) > 200 or len(topic) > 100:
                    raise ValueError("Title and topic must fit within 200 and 100 characters")
                captured_on = date.fromisoformat(captured).isoformat()
                if captured != captured_on:
                    raise ValueError("Capture date must be in YYYY-MM-DD format")
                url = normalize_source_url(url)
                if (url, captured_on) in identities:
                    raise ValueError("A source address and capture date may appear only once")
                relative = Path(name)
                if relative.drive or relative.root or ".." in relative.parts:
                    raise ValueError("Source files must be relative to the CSV directory")
                source = (base / relative).resolve()
                if not source.is_relative_to(base):
                    raise ValueError("Source files must be relative to the CSV directory")
                raw, prepared, extension = prepare_file(source)
            except (OSError, ValueError) as error:
                raise ValueError(f"Snapshot {number}: {error}") from None
            identities.add((url, captured_on))
            topics[topic] += 1
            digest = hashlib.sha256(prepared).hexdigest()
            key = (extension, digest)
            filename = unique_files.get(key)
            duplicate = filename is not None
            if filename is None:
                filename = f"snapshot-{number:03d}-{digest[:12]}{extension}"
                (bundle / filename).write_bytes(prepared)
                unique_files[key] = filename
            snapshots.append({
                "title": title, "topic": topic, "url": url, "captured_on": captured_on,
                "file": filename, "input_sha256": hashlib.sha256(raw).hexdigest(),
                "snapshot_sha256": digest, "duplicate_content": duplicate,
            })
        if not snapshots:
            raise ValueError("Manifest has no snapshots")
        inventory = {
            "snapshot_count": len(snapshots), "unique_file_count": len(unique_files),
            "snapshots_by_topic": dict(topics), "snapshots": snapshots,
        }
        (bundle / "inventory.json").write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        bundle.rename(output)
    print(f"Prepared {len(snapshots)} snapshots, {len(unique_files)} unique files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--manifest", action="store_true", help="Prepare snapshots from a CSV file")
    args = parser.parse_args()
    try:
        if args.manifest:
            prepare_batch(args.input, args.output)
        else:
            if args.output.suffix.lower() != ".txt":
                raise ValueError("Output must have a .txt suffix")
            data = extract_html(read_bounded(args.input, MAX_HTML_BYTES))
            with args.output.open("xb") as handle:
                handle.write(data)
    except (OSError, ValueError, csv.Error) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
