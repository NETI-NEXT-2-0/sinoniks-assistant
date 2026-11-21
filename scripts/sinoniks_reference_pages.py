import re
from pathlib import Path


def extract_pages(path: Path, source: bytes) -> dict[int, str]:
    parts = re.split(r"(?m)^--- (\d+) ---[^\S\n]*$", source.decode("utf-8-sig"))
    if len(parts) < 3 or parts[0].strip():
        raise ValueError(f"{path.name}: expected text with numbered page markers")
    pages = {}
    for number, content in zip(parts[1::2], parts[2::2], strict=True):
        page = int(number)
        if page <= 0 or page in pages:
            raise ValueError(f"{path.name}: invalid or duplicate page {page}")
        pages[page] = content.strip()
    return pages
