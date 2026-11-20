#!/usr/bin/env python3
"""Reproducible synthetic Russian corpus; run with parser/.venv/bin/python."""

import json
import os
import subprocess
import tempfile
from pathlib import Path


def main():
    from docx import Document
    from PIL import Image, ImageDraw, ImageFont

    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.setdefault("PARTNER_PARSER_MODELS", str(root / ".local/parser-models"))
    if not (Path(env["PARTNER_PARSER_MODELS"]) / "manifest.json").exists():
        raise RuntimeError("Provision parser models and checksum manifest first")
    report = {}
    with tempfile.TemporaryDirectory(prefix="partner-v4-") as directory:
        base = Path(directory)
        document = Document()
        document.add_heading("Программа обучения", 0)
        document.add_paragraph("Модуль Python. Код EDU-204. Продолжительность 24 часа.")
        table = document.add_table(rows=3, cols=2)
        table.style = "Table Grid"
        for row, values in zip(table.rows, [("Модуль", "Часы"), ("Python", "24"), ("SQL", "16")]):
            for cell, value in zip(row.cells, values):
                cell.text = value
        document.add_paragraph("Примечание 1: материалы доступны участникам курса.")
        document.save(base / "course.docx")
        image = Image.new("RGB", (1654, 2339), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        draw.text((90, 100), "Программа обучения", font=font, fill="black")
        draw.text((90, 190), "Код EDU-204. Продолжительность 24 часа.", font=font, fill="black")
        for y in (320, 420, 520, 620):
            draw.line((90, y, 1300, y), fill="black", width=3)
        for x in (90, 900, 1300):
            draw.line((x, 320, x, 620), fill="black", width=3)
        for y, values in zip(
            (345, 445, 545), [("Модуль", "Часы"), ("Python", "24"), ("SQL", "16")]
        ):
            draw.text((110, y), values[0], font=font, fill="black")
            draw.text((930, y), values[1], font=font, fill="black")
        draw.text((90, 750), "Примечание 1: материалы доступны", font=font, fill="black")
        draw.text((90, 810), "участникам курса.", font=font, fill="black")
        image.save(base / "course.png")
        image.save(base / "course-scan.pdf", "PDF", resolution=150)
        for name in ("course.docx", "course.png", "course-scan.pdf"):
            output = base / (name + "-output")
            output.mkdir()
            subprocess.run(
                [str(root / "scripts/run-parser.sh"), str(base / name), str(output)],
                env=env,
                check=True,
                timeout=630,
                stdout=subprocess.DEVNULL,
            )
            data = json.loads((output / "canonical-docling.json").read_text())
            texts = [item["text"] for item in data.get("texts", [])]
            cells = [
                cell["text"]
                for table in data.get("tables", [])
                for cell in table["data"]["table_cells"]
            ]
            combined = "\n".join(texts + cells)
            assert "24" in combined and "Python" in combined, "Required course values missing"
            assert "Примечание" in combined, "Note missing"
            positions = [prov for item in data.get("texts", []) for prov in item.get("prov", [])]
            if name != "course.docx":
                assert positions and all(p["page_no"] == 1 and p.get("bbox") for p in positions)
            else:
                assert len(cells) == 6, "DOCX table structure lost"
            report[name] = {
                "text_items": len(texts),
                "table_cells": len(cells),
                "exact_identifier": "EDU-204" in combined,
                "note_present": True,
                "page_bbox_count": len(positions),
            }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
