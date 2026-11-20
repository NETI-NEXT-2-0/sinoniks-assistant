import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "parser_artifacts", Path(__file__).resolve().parents[2] / "parser" / "artifacts.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_failed_serialization_preserves_previous_complete_artifact(tmp_path):
    destination = tmp_path / "canonical.json"
    destination.write_text('{"previous":true}')
    with pytest.raises(ValueError):
        module.write_json_atomic(destination, {"unfinished": float("nan")})
    assert json.loads(destination.read_text()) == {"previous": True}
    assert list(tmp_path.iterdir()) == [destination]
    module.write_json_atomic(destination, {"text": "Материалы курса"})
    assert json.loads(destination.read_text()) == {"text": "Материалы курса"}
    assert list(tmp_path.iterdir()) == [destination]


def test_failed_replace_preserves_original_and_removes_temporary_file(tmp_path, monkeypatch):
    destination = tmp_path / "canonical.json"
    destination.write_text('{"previous":true}')

    def fail(*args):
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError):
        module.write_json_atomic(destination, {"new": True})
    assert json.loads(destination.read_text()) == {"previous": True}
    assert list(tmp_path.iterdir()) == [destination]
