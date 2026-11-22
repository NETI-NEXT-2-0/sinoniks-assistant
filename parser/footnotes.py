"""Exact DOCX footnote relationships from OOXML, independent of layout heuristics."""

import zipfile
from pathlib import Path

from defusedxml import ElementTree

NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_footnotes(source: Path) -> list[dict[str, object]]:
    with zipfile.ZipFile(source) as archive:
        if "word/footnotes.xml" not in archive.namelist():
            return []
        notes = ElementTree.fromstring(archive.read("word/footnotes.xml"))
        document = ElementTree.fromstring(archive.read("word/document.xml"))
    by_id = {
        note.attrib[NS + "id"]: "".join(t.text or "" for t in note.iter(NS + "t"))
        for note in notes
        if note.attrib.get(NS + "type") is None
    }
    result = []
    for ordinal, paragraph in enumerate(document.iter(NS + "p")):
        for reference in paragraph.iter(NS + "footnoteReference"):
            identifier = reference.attrib[NS + "id"]
            if identifier not in by_id:
                raise ValueError("missing_footnote_target")
            result.append(
                {
                    "note_id": identifier,
                    "paragraph_ordinal": ordinal,
                    "text": by_id[identifier],
                    "locator": "word/footnotes.xml#" + identifier,
                }
            )
    return result
