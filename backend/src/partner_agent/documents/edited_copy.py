"""Hash-bound, explicitly reviewed text copy; source artifacts remain immutable."""

import hashlib
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from partner_agent.documents.limits import MAX_EVIDENCE_CHARS
from partner_agent.infra.errors import APIError

EDITED_FORMAT = "curator-edited-v1"


class ReviewedCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = Field(default=EDITED_FORMAT, pattern="^curator-edited-v1$")
    document_id: UUID
    version_id: UUID
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=1_000_000)


def copy_key(document_id: UUID, version_id: UUID, digest: str) -> str:
    return f"{document_id}/{version_id}/reviewed/{digest}.json"


def render_copy(copy: ReviewedCopy, digest: str) -> list[tuple[UUID, str, str, dict[str, Any]]]:
    if not copy.text.strip():
        raise APIError(422, "empty_disclosure", "The reviewed copy is empty")
    # Keep every character of the approved copy, including paragraph separators.
    # Citation locators refer to copy offsets, never fabricated original page coordinates.
    result = []
    offset = 0
    while offset < len(copy.text):
        end = min(offset + MAX_EVIDENCE_CHARS, len(copy.text))
        if end < len(copy.text):
            boundary = max(copy.text.rfind("\n", offset, end), copy.text.rfind(" ", offset, end))
            if boundary > offset:
                end = boundary + 1
        text = copy.text[offset:end]
        block_id = uuid5(copy.version_id, f"{EDITED_FORMAT}:{digest}:{offset}")
        locator = {
            "reviewed_copy": True,
            "copy_sha256": digest,
            "canonical_sha256": copy.canonical_sha256,
            "start": offset,
            "end": end,
        }
        result.append((block_id, text, hashlib.sha256(text.encode()).hexdigest(), locator))
        offset = end
    if len(result) > 1000:
        raise APIError(422, "too_many_blocks", "The reviewed copy is too large")
    return result
