#!/usr/bin/env python3
"""Generate synthetic Cyrillic documents using the parser image's dependencies."""

import argparse
from pathlib import Path

TABLE = (("Модуль", "Часы"), ("Python", "24"), ("SQL", "16"))
FOOTNOTE = "Материалы доступны только участникам курса."


def write_docx(output: Path) -> None:
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document = Document()
    document.add_heading("Программа обучения", 0)
    paragraph = document.add_paragraph("Регламент курса EDU-204.")
    run = paragraph.add_run()
    reference = OxmlElement("w:footnoteReference")
    reference.set(qn("w:id"), "1")
    run._r.append(reference)
    source = (
        '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:footnote w:id="1"><w:p><w:r><w:t>' + FOOTNOTE + "</w:t></w:r></w:p>"
        "</w:footnote></w:footnotes>"
    )
    footnotes = Part(
        PackURI("/word/footnotes.xml"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
        source.encode(),
        document.part.package,
    )
    document.part.relate_to(footnotes, RELATIONSHIP_TYPE.FOOTNOTES)
    table = document.add_table(rows=len(TABLE), cols=2)
    table.style = "Table Grid"
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    for row, values in zip(table.rows, TABLE, strict=True):
        for cell, text in zip(row.cells, values, strict=True):
            cell.text = text
    document.add_paragraph("Служебная заметка: DEMO-PRIVATE-731.")
    document.save(output)


def write_scans(output: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
    first = Image.new("RGB", (1654, 1700), "white")
    draw = ImageDraw.Draw(first)
    draw.text((95, 90), "Программа обучения", font=font, fill="black")
    draw.text((95, 190), "Регламент курса EDU-204.", font=font, fill="black")
    for y in (320, 445, 570, 695):
        draw.line((95, y, 1320, y), fill="black", width=4)
    for x in (95, 900, 1320):
        draw.line((x, 320, x, 695), fill="black", width=4)
    for y, values in zip((350, 475, 600), TABLE, strict=True):
        for x, value in zip((120, 935), values, strict=True):
            draw.text((x, y), value, font=font, fill="black")
    draw.text(
        (95, 805), "Служебная заметка: DEMO-PRIVATE-731.", font=font, fill="black"
    )
    second = Image.new("RGB", first.size, "white")
    ImageDraw.Draw(second).text((95, 120), FOOTNOTE, font=font, fill="black")
    first.save(output / "course-scan.png")
    first.save(output / "course-scan-150.png", dpi=(150, 150))
    first.save(
        output / "course-scan.pdf",
        "PDF",
        resolution=150,
        save_all=True,
        append_images=[second],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    output = parser.parse_args().output
    output.mkdir(parents=True, exist_ok=True)
    write_docx(output / "course.docx")
    write_scans(output)


if __name__ == "__main__":
    main()
