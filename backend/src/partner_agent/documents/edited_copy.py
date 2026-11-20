"""Hash-bound, explicitly reviewed text copy; source artifacts remain immutable."""

import hashlib
import re
from collections.abc import Iterator
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from partner_agent.documents.limits import MAX_EVIDENCE_CHARS
from partner_agent.infra.errors import APIError

EDITED_FORMAT = "curator-edited-v1"
MARKDOWN_LOCATORS = "markdown-atx-v1"
MAX_SECTION_TITLE = 200
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
HEADING = re.compile(r" {0,3}#{1,6}[ \t]+(.+)")


class ReviewedCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = Field(default=EDITED_FORMAT, pattern="^curator-edited-v1$")
    document_id: UUID
    version_id: UUID
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=1_000_000)
    locator_profile: Literal["markdown-atx-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )


def copy_key(document_id: UUID, version_id: UUID, digest: str) -> str:
    return f"{document_id}/{version_id}/reviewed/{digest}.json"


def markdown_sections(text: str) -> list[tuple[int, str | None]]:
    starts: dict[int, str | None] = {0: None}
    fence = ""
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        marker = FENCE.fullmatch(content)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = ""
        elif marker:
            fence = marker[1]
        elif heading := HEADING.fullmatch(content):
            title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[1]).strip()
            starts[offset] = title if 0 < len(title) <= MAX_SECTION_TITLE else None
        offset += len(line)
    return list(starts.items())


def copy_ranges(copy: ReviewedCopy) -> Iterator[tuple[int, int, str | None]]:
    sections = markdown_sections(copy.text) if copy.locator_profile else [(0, None)]
    for index, (offset, title) in enumerate(sections):
        limit = sections[index + 1][0] if index + 1 < len(sections) else len(copy.text)
        while offset < limit:
            end = min(offset + MAX_EVIDENCE_CHARS, limit)
            if end < limit:
                boundary = max(
                    copy.text.rfind("\n", offset, end), copy.text.rfind(" ", offset, end),
                )
                if boundary > offset:
                    end = boundary + 1
            yield offset, end, title
            offset = end


def render_copy(copy: ReviewedCopy, digest: str) -> list[tuple[UUID, str, str, dict[str, Any]]]:
    if not copy.text.strip():
        raise APIError(422, "empty_disclosure", "The reviewed copy is empty")
    # Citation locators refer to copy offsets, never fabricated original page coordinates.
    result = []
    for offset, end, title in copy_ranges(copy):
        text = copy.text[offset:end]
        block_id = uuid5(copy.version_id, f"{EDITED_FORMAT}:{digest}:{offset}")
        locator = {
            "reviewed_copy": True,
            "copy_sha256": digest,
            "canonical_sha256": copy.canonical_sha256,
            "start": offset,
            "end": end,
        }
        if title is not None:
            locator["section"] = title
        result.append((block_id, text, hashlib.sha256(text.encode()).hexdigest(), locator))
    if len(result) > 1000:
        raise APIError(422, "too_many_blocks", "The reviewed copy is too large")
    return result
