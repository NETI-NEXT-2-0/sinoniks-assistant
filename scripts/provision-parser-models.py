#!/usr/bin/env python3
"""Download CPU parser models before sandbox jobs; emit content checksums."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    from docling.utils.model_downloader import download_models

    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    download_models(
        args.directory,
        with_layout=True,
        with_tableformer=True,
        with_code_formula=False,
        with_picture_classifier=False,
        with_rapidocr=False,
    )
    manifest = {}
    for path in sorted(args.directory.rglob("*")):
        if not path.is_file() or ".cache" in path.parts or path.name == "manifest.json":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest[str(path.relative_to(args.directory))] = digest.hexdigest()
    (args.directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"parser_model_files={len(manifest)}")


if __name__ == "__main__":
    main()
