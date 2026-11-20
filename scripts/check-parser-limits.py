#!/usr/bin/env python3
"""Real image/PDF preflight boundary tests using the isolated parser environment."""

import importlib.util
import tempfile
from pathlib import Path


def main():
    import pypdfium2 as pdfium
    from PIL import Image

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("validation", root / "parser/validate.py")
    validation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validation)
    with tempfile.TemporaryDirectory(prefix="partner-parser-limits-") as directory:
        base = Path(directory)
        pdf = pdfium.PdfDocument.new()
        try:
            for _ in range(201):
                page = pdf.new_page(100, 100)
                page.close()
            pdf.save(base / "too-many.pdf")
        finally:
            pdf.close()
        try:
            validation.validate_source(base / "too-many.pdf")
        except ValueError as error:
            assert str(error) == "pdf_page_limit"
        else:
            raise AssertionError("201-page PDF accepted")
        Image.new("RGB", (5000, 4001), "white").save(base / "large.png")
        try:
            validation.validate_source(base / "large.png")
        except ValueError as error:
            assert str(error) == "image_pixel_limit"
        else:
            raise AssertionError("Oversized image accepted")
        Image.new("RGB", (5000, 4000), "white").save(base / "boundary.png")
        validation.validate_source(base / "boundary.png")
        Image.new("RGB", (10000, 5000), "white").save(base / "bomb.png")
        try:
            validation.validate_source(base / "bomb.png")
        except ValueError as error:
            assert str(error) == "image_pixel_limit"
        else:
            raise AssertionError("Decompression bomb accepted")
        Image.new("RGB", (10, 10), "white").save(base / "wrong.png", format="GIF")
        try:
            validation.validate_source(base / "wrong.png")
        except ValueError as error:
            assert str(error) == "image_format_mismatch"
        else:
            raise AssertionError("Disguised GIF accepted")
    print("pdf_201_page_rejection_image_pixel_boundary_passed")


if __name__ == "__main__":
    main()
