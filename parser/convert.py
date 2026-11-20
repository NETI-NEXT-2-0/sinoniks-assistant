#!/usr/bin/env python3
"""Bounded CPU conversion entrypoint; run inside a network-disabled container."""

import argparse
import importlib.metadata
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from artifacts import write_json_atomic
from validate import MAX_BYTES, validate_source


@contextmanager
def conversion_source(source: Path) -> Iterator[tuple[Path, bool]]:
    """Use an OCR scale for a single PNG whose original density is unspecified."""
    if source.suffix.lower() == ".png":
        from PIL import Image

        with Image.open(source) as image:
            aspect = image.info.get("aspect", (1, 1))
            if (
                image.format == "PNG"
                and image.n_frames == 1
                and image.info.get("dpi") is None
                and aspect[0] == aspect[1]
            ):
                with TemporaryDirectory(prefix="partner-png-") as directory:
                    adjusted = Path(directory) / "scan.png"
                    image.save(adjusted, dpi=(150, 150), exif=image.info.get("exif"))
                    if adjusted.stat().st_size <= MAX_BYTES:
                        yield adjusted, True
                        return
    yield source, False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    validate_source(args.source)
    os.environ["OMP_NUM_THREADS"] = "4"
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions
    from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

    options = PdfPipelineOptions()
    artifacts = os.environ.get("PARTNER_PARSER_ARTIFACTS")
    if artifacts:
        from model_integrity import verify_models

        verify_models(Path(artifacts))
        options.artifacts_path = Path(artifacts)
    options.do_ocr = True
    options.ocr_options = TesseractCliOcrOptions(lang=["rus", "eng"])
    options.enable_remote_services = False
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX, InputFormat.HTML, InputFormat.IMAGE],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
        },
    )
    with conversion_source(args.source) as (source, assumed_density):
        result = converter.convert(source, max_num_pages=200, max_file_size=MAX_BYTES)
        if result.status.value != "success":
            raise ValueError("extraction_incomplete")
        exported = result.document.export_to_dict()
    exported["partner_parser_version"] = "docling-" + importlib.metadata.version("docling")
    if assumed_density:
        exported["partner_quality_warnings"] = ["png_dpi_assumed"]
    if args.source.suffix.lower() == ".pdf":
        from pdf_quality import native_text_warnings

        exported["partner_quality_warnings"] = native_text_warnings(args.source)
    if args.source.suffix.lower() == ".docx":
        from footnotes import docx_footnotes

        exported["partner_footnote_relationships"] = docx_footnotes(args.source)
    write_json_atomic(args.output, exported)


if __name__ == "__main__":
    main()
