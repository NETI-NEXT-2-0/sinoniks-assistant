"""Validate labels for saved pages. The application never resolves these URLs."""

import re
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

_DOMAIN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)


def normalize_source_url(value: str) -> str:
    if not value.startswith("https://") or any(
        char.isspace() or not char.isprintable() or char == "\\" for char in value
    ):
        raise ValueError("invalid_source_url")
    try:
        parts = urlsplit(value)
        if (
            not parts.netloc or "@" in parts.netloc or "%" in parts.netloc
            or parts.port not in (None, 443)
        ):
            raise ValueError("invalid_source_url")
        url = HttpUrl(value)
    except (ValueError, ValidationError):
        raise ValueError("invalid_source_url") from None
    host = url.host or ""
    normalized = str(url)
    if (
        not _DOMAIN.fullmatch(host)
        or host.endswith((".localhost", ".local", ".internal"))
        or len(normalized) > 2048
    ):
        raise ValueError("invalid_source_url")
    return normalized
