"""Verify provisioned model bytes before any offline engine loads them."""

import hashlib
import json
from pathlib import Path


def verify_models(directory: Path) -> None:
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("model_manifest_missing")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("model_manifest_invalid")
    base = directory.resolve()
    for relative, expected in manifest.items():
        path = (base / relative).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise ValueError("model_file_missing")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("model_checksum_mismatch")
