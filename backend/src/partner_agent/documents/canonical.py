"""Deterministic UTF-8 adapter with immutable provenance and exact source offsets."""

import hashlib
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_FILE_BYTES = 25 * 1024 * 1024


class CanonicalError(ValueError):
    """Safe technical failure code; never carries source content."""


class Locator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    line: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)


class Block(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    ordinal: int
    kind: Literal["paragraph"] = "paragraph"
    text: str
    text_sha256: str
    locator: Locator


class CanonicalDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    document_id: UUID
    version_id: UUID
    source_sha256: str
    parser_version: Literal["utf8-lines-v1"] = "utf8-lines-v1"
    pipeline_version: Literal["canonical-v1"] = "canonical-v1"
    media_type: Literal["text/plain", "text/markdown"]
    language: Literal["und"] = "und"
    warnings: tuple[str, ...] = ()
    blocks: tuple[Block, ...]

    @model_validator(mode="after")
    def valid_integrity(self) -> "CanonicalDocument":
        if len(self.source_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.source_sha256
        ):
            raise ValueError("invalid_source_hash")
        if not self.blocks or len(self.blocks) > 100000:
            raise ValueError("invalid_block_count")
        last_line = 0
        last_end = 0
        for ordinal, block in enumerate(self.blocks):
            loc = block.locator
            digest = hashlib.sha256(block.text.encode()).hexdigest()
            if (
                block.ordinal != ordinal or block.text_sha256 != digest
                or block.id != uuid5(self.version_id, f"line:{loc.line}:{digest}")
                or loc.line <= last_line or loc.char_start < last_end
                or loc.char_end - loc.char_start != len(block.text)
                or not block.text.strip()
            ):
                raise ValueError("invalid_block_provenance")
            last_line, last_end = loc.line, loc.char_end
        return self


def extract_utf8(
    data: bytes, document_id: UUID, version_id: UUID, media_type: str
) -> CanonicalDocument:
    if media_type not in ("text/plain", "text/markdown"):
        raise CanonicalError("unsupported_media_type")
    if len(data) > MAX_FILE_BYTES:
        raise CanonicalError("file_too_large")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise CanonicalError("invalid_utf8") from None
    if "\x00" in text:
        raise CanonicalError("binary_content")
    if not text.strip("\ufeff \t\r\n"):
        raise CanonicalError("empty_document")
    blocks: list[Block] = []
    offset = 0
    # Offsets refer to decoded original source, including BOM and CRLF characters.
    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        content = line.rstrip("\r\n")
        start = offset
        if line_number == 1 and content.startswith("\ufeff"):
            content = content[1:]
            start += 1
        if content.strip():
            if len(blocks) >= 100000:
                raise CanonicalError("element_limit")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            blocks.append(
                Block(
                    id=uuid5(version_id, f"line:{line_number}:{digest}"),
                    ordinal=len(blocks),
                    text=content,
                    text_sha256=digest,
                    locator=Locator(
                        line=line_number, char_start=start, char_end=start + len(content)
                    ),
                )
            )
        offset += len(line)
    return CanonicalDocument(
        document_id=document_id,
        version_id=version_id,
        source_sha256=hashlib.sha256(data).hexdigest(),
        media_type=media_type,
        blocks=tuple(blocks),
    )
