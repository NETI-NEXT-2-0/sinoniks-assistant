"""Conservative review hints; no text correction or permission decisions."""

import re

TOKEN = re.compile(r"[\w]+(?:[.,/-][\w]+)*", re.UNICODE)


def has_mixed_script_identifier(text: str) -> bool:
    """Flag numeric tokens containing both ASCII Latin and Cyrillic letters."""
    for token in TOKEN.findall(text):
        latin = any("a" <= char <= "z" or "A" <= char <= "Z" for char in token)
        cyrillic = any("\u0400" <= char <= "\u052f" for char in token)
        if latin and cyrillic and any(char.isdigit() for char in token):
            return True
    return False
