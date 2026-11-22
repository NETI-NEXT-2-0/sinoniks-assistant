"""Flag native PDF glyph-order inversions; never silently rewrite source text."""

from pathlib import Path


def native_text_warnings(source: Path) -> list[str]:
    import pypdfium2 as pdfium

    warnings = set()
    document = pdfium.PdfDocument(source)
    try:
        for index in range(len(document)):
            page = document[index]
            text = page.get_textpage()
            try:
                previous = None
                for offset in range(text.count_chars()):
                    value = text.get_text_range(offset, 1)
                    if not value.strip():
                        continue
                    left, bottom, right, top = text.get_charbox(offset)
                    center = (bottom + top) / 2
                    if previous is not None:
                        old_left, old_center, old_height = previous
                        if abs(center - old_center) <= max(top - bottom, old_height) * 0.6:
                            if left + 2 < old_left:
                                warnings.add("native_text_order_uncertain")
                    previous = (left, center, top - bottom)
            finally:
                text.close()
                page.close()
    finally:
        document.close()
    return sorted(warnings)
