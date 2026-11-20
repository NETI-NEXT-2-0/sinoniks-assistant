"""Input guards run without loading Docling or its model dependencies."""

import importlib.util
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "parser_validation", Path(__file__).resolve().parents[2] / "parser" / "validate.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_docx_expanded_size_and_macros_rejected(tmp_path, monkeypatch):
    path = tmp_path / "source.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 1000)
        archive.writestr("[Content_Types].xml", "<Types/>")
    monkeypatch.setattr(module, "MAX_EXPANDED_BYTES", 100)
    with pytest.raises(ValueError, match="expanded_document_limit"):
        module.validate_source(path)
    monkeypatch.setattr(module, "MAX_EXPANDED_BYTES", 10000)
    module.validate_source(path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("word/vbaProject.bin", "synthetic")
    with pytest.raises(ValueError, match="macro_document_forbidden"):
        module.validate_source(path)


def test_invalid_office_and_empty_source_rejected(tmp_path):
    path = tmp_path / "invalid.docx"
    path.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="invalid_docx"):
        module.validate_source(path)
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="invalid_source_size"):
        module.validate_source(path)
