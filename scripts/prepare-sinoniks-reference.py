import argparse
import hashlib
import json
from pathlib import Path

from sinoniks_reference_pages import extract_pages

GUIDES = {
    "administrator": (
        "RU.33654484.0003-01 91 01",
        "https://it-bastion.com/upload/iblock/785/kpalv2pauvvwartegbi5drh1am9lug6p.pdf",
        (13, 34, 35, 36, 40, 45, 46, 48, 52, 54),
    ),
    "user": (
        "RU.33654484.0003-01 90 01",
        "https://it-bastion.com/upload/iblock/122/8ld95dclnr4bvob9xvpgffi8dpxfsx9d.pdf",
        (11,),
    ),
    "installation": (
        "RU.33654484.0003-01 91 02",
        "https://it-bastion.com/upload/iblock/fcc/lng00ne74y8x3qzcj0cnppy3yehzsa1h.pdf",
        (6, 9),
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    for guide in GUIDES:
        parser.add_argument(guide, type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    files = {}
    manifest = []
    try:
        for guide, (designation, url, selected) in GUIDES.items():
            path = getattr(args, guide)
            source = path.read_bytes()
            pages = extract_pages(path, source)
            if not any(designation in page for page in pages.values()):
                raise ValueError(f"{path.name}: missing document designation {designation}")
            missing = [page for page in selected if not pages.get(page)]
            if missing:
                raise ValueError(f"{path.name}: missing text on physical pages {missing}")
            excerpt = "\n\n".join(
                f"--- {page} ---\n" + "\n".join(line.rstrip() for line in pages[page].splitlines())
                for page in selected
            )
            name = f"{guide}.txt"
            content = (excerpt + "\n").encode("utf-8")
            files[name] = content
            manifest.append(
                {
                    "designation": designation,
                    "source_url": url,
                    "physical_pages": selected,
                    "extracted_text_sha256": hashlib.sha256(source).hexdigest(),
                    "file": name,
                    "file_sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        args.output.mkdir()
        for name, content in files.items():
            (args.output / name).write_bytes(content)
        (args.output / "manifest.json").write_bytes(
            (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Prepared {len(manifest)} guides and {sum(len(s[2]) for s in GUIDES.values())} pages")


if __name__ == "__main__":
    main()
