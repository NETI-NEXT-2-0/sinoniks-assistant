#!/usr/bin/env python3
"""Report exact mixed-script synthetic identifier OCR in the isolated parser.

Run with parser/.venv/bin/python after provisioning models and building the parser image.
This is a diagnostic, not a general OCR quality score or an automatic correction.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

IDENTIFIERS = ("AB-123", "АВ-456", "EDU-204", "SECRET-782", "KPI-2026", "КРI-2036")
TOKEN = re.compile(r"[\w]+(?:[.,/-][\w]+)*", re.UNICODE)


def main():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.setdefault("PARTNER_PARSER_MODELS", str(root / ".local/parser-models"))
    if not (Path(env["PARTNER_PARSER_MODELS"]) / "manifest.json").is_file():
        raise RuntimeError("Provision parser models and checksum manifest first")
    with tempfile.TemporaryDirectory(prefix="partner-v4-identifiers-") as temporary:
        base = Path(temporary)
        source, output = base / "synthetic.png", base / "output"
        output.mkdir()
        image = Image.new("RGB", (1800, 1050), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
        draw.text((80, 60), "Проверка учебных кодов", font=font, fill="black")
        for position, identifier in enumerate(IDENTIFIERS):
            draw.text((80, 180 + position * 110), f"Код {identifier}", font=font, fill="black")
        image.save(source, "PNG", dpi=(150, 150))
        subprocess.run(
            [str(root / "scripts/run-parser.sh"), str(source), str(output)],
            env=env, check=True, timeout=630, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        data = json.loads((output / "canonical-docling.json").read_text())
        texts = data.get("texts", [])
        report = {}
        for identifier in IDENTIFIERS:
            matches = [item for item in texts if identifier in TOKEN.findall(item.get("text", ""))]
            report[identifier] = {
                "exact_token": bool(matches),
                "page_bbox": any(
                    position.get("page_no") == 1 and bool(position.get("bbox"))
                    for item in matches for position in item.get("prov", [])
                ),
            }
        assert texts, "Parser returned no text blocks"
    print(json.dumps({"fixture": "mixed-script-synthetic-v1", "identifiers": report},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
