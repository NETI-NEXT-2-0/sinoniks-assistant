#!/usr/bin/env python3
"""Check a synthetic encrypted PDF is rejected locally and in the parser sandbox.

Requires Ghostscript, the parser venv and the prebuilt partner-agent-parser:stage3 image.
All documents and the password in this probe are synthetic and temporary.
"""

import importlib.util
import subprocess
import tempfile
from pathlib import Path

import pypdfium2 as pdfium


def main():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("validation", root / "parser/validate.py")
    assert spec and spec.loader
    validation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validation)
    with tempfile.TemporaryDirectory(prefix="partner-encrypted-pdf-") as directory:
        base = Path(directory)
        plain, encrypted = base / "plain.pdf", base / "encrypted.pdf"
        pdf = pdfium.PdfDocument.new()
        try:
            page = pdf.new_page(100, 100)
            page.close()
            pdf.save(plain)
        finally:
            pdf.close()
        validation.validate_source(plain)
        subprocess.run(
            ["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
             "-sOwnerPassword=synthetic-owner", "-sUserPassword=synthetic-open",
             f"-sOutputFile={encrypted}", str(plain)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
        # Ensure the fixture is actually encrypted and readable with the test password.
        unlocked = pdfium.PdfDocument(encrypted, password="synthetic-open")
        try:
            assert len(unlocked) == 1
        finally:
            unlocked.close()
        try:
            validation.validate_source(encrypted)
        except ValueError as error:
            assert str(error) == "invalid_or_encrypted_pdf"
        else:
            raise AssertionError("encrypted PDF accepted by local preflight")
        output = base / "output"
        output.mkdir()
        result = subprocess.run(
            [str(root / "scripts/run-parser.sh"), str(encrypted), str(output)],
            check=False, capture_output=True, text=True, timeout=90,
        )
        assert result.returncode != 0, "encrypted PDF accepted by sandbox"
        assert "invalid_or_encrypted_pdf" in result.stderr, "unexpected sandbox failure"
        assert not list(output.iterdir()), "sandbox produced an artifact for encrypted PDF"
    print("encrypted_pdf_local_and_isolated_rejection_passed")


if __name__ == "__main__":
    main()
