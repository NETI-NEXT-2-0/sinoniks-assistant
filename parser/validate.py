"""Reject unsafe/oversized parser inputs before importing document engines."""

import warnings
import zipfile
from pathlib import Path

MAX_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 20_000_000


def validate_source(source: Path) -> None:
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_BYTES:
        raise ValueError("invalid_source_size")
    suffix = source.suffix.lower()
    if suffix not in {".pdf", ".docx", ".html", ".png", ".jpg", ".jpeg"}:
        raise ValueError("unsupported_media_type")
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
                    raise ValueError("expanded_document_limit")
                names = {e.filename for e in entries}
                if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                    raise ValueError("invalid_docx")
                if any("vbaproject" in name.casefold() for name in names):
                    raise ValueError("macro_document_forbidden")
                if any(e.flag_bits & 1 for e in entries):
                    raise ValueError("encrypted_document_forbidden")
        except zipfile.BadZipFile:
            raise ValueError("invalid_docx") from None
    elif suffix == ".pdf":
        import pypdfium2 as pdfium

        try:
            document = pdfium.PdfDocument(source)
            try:
                if len(document) > 200:
                    raise ValueError("pdf_page_limit")
            finally:
                document.close()
        except pdfium.PdfiumError:
            raise ValueError("invalid_or_encrypted_pdf") from None
    elif suffix in {".png", ".jpg", ".jpeg"}:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source) as image:
                    if image.width * image.height > MAX_PIXELS:
                        raise ValueError("image_pixel_limit")
                    if image.format not in {"PNG", "JPEG"}:
                        raise ValueError("image_format_mismatch")
                    image.verify()
        except (Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise ValueError("image_pixel_limit") from None
