import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "model_integrity", Path(__file__).resolve().parents[2] / "parser" / "model_integrity.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_model_manifest_detects_corruption_and_path_escape(tmp_path):
    model = tmp_path / "model.bin"
    model.write_bytes(b"synthetic weights")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"model.bin": hashlib.sha256(model.read_bytes()).hexdigest()}))
    module.verify_models(tmp_path)
    model.write_bytes(b"corrupted weights")
    with pytest.raises(ValueError, match="model_checksum_mismatch"):
        module.verify_models(tmp_path)
    manifest.write_text(json.dumps({"../outside.bin": "a" * 64}))
    with pytest.raises(ValueError, match="model_file_missing"):
        module.verify_models(tmp_path)
