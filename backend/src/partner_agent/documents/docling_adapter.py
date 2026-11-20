"""Normalize Docling JSON without importing the parser into the API environment."""

import hashlib
import math
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from partner_agent.documents.canonical import CanonicalError


class SourcePosition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    element_path: str
    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    origin: Literal["TOPLEFT", "BOTTOMLEFT"] | None = None
    page_width: float | None = Field(default=None, gt=0)
    page_height: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_geometry(self) -> "SourcePosition":
        values = [value for value in (self.page_width, self.page_height) if value is not None]
        if self.bbox is not None:
            values.extend(self.bbox)
            left, top, right, bottom = self.bbox
            if self.page is None or self.origin is None or left > right:
                raise ValueError("invalid_bbox")
            if self.origin == "TOPLEFT" and top > bottom:
                raise ValueError("invalid_bbox_orientation")
            if self.origin == "BOTTOMLEFT" and top < bottom:
                raise ValueError("invalid_bbox_orientation")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("nonfinite_geometry")
        return self


class TableCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(ge=1)
    column_span: int = Field(ge=1)
    column_header: bool
    row_header: bool
    text: str


class SourceBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    ordinal: int
    kind: str
    text: str
    text_sha256: str
    positions: tuple[SourcePosition, ...]
    cells: tuple[TableCell, ...] = ()
    related_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_text_integrity(self) -> "SourceBlock":
        if self.ordinal < 0 or not self.positions:
            raise ValueError("invalid_block_provenance")
        if hashlib.sha256(self.text.encode()).hexdigest() != self.text_sha256:
            raise ValueError("block_checksum_mismatch")
        return self


class FootnoteRelationship(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    note_id: str
    paragraph_ordinal: int = Field(ge=0)
    text: str
    locator: str


class ParsedDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    document_id: UUID
    version_id: UUID
    source_sha256: str
    parser_version: str
    pipeline_version: Literal["docling-adapter-v1"] = "docling-adapter-v1"
    blocks: tuple[SourceBlock, ...]
    warnings: tuple[str, ...]
    footnote_relationships: tuple[FootnoteRelationship, ...] = ()

    @model_validator(mode="after")
    def valid_document_integrity(self) -> "ParsedDocument":
        if len(self.source_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in self.source_sha256
        ):
            raise ValueError("invalid_source_hash")
        if not self.blocks or len(self.blocks) > 100000:
            raise ValueError("invalid_block_count")
        if len({block.id for block in self.blocks}) != len(self.blocks):
            raise ValueError("duplicate_block_id")
        if [block.ordinal for block in self.blocks] != list(range(len(self.blocks))):
            raise ValueError("invalid_block_order")
        paths: set[str] = set()
        for block in self.blocks:
            primary = block.positions[0].element_path
            expected = uuid5(self.version_id, f"{primary}:{block.text_sha256}")
            if block.id != expected:
                raise ValueError("block_version_identity_mismatch")
            if primary in paths:
                raise ValueError("duplicate_block_locator")
            paths.add(primary)
            if any(position.element_path != primary for position in block.positions):
                raise ValueError("inconsistent_block_locator")
        for block in self.blocks:
            if any(path not in paths for path in block.related_paths):
                raise ValueError("unresolved_block_relationship")
        return self


def normalize_docling(
    data: dict[str, Any],
    document_id: UUID,
    version_id: UUID,
    source_sha256: str,
    parser_version: str,
) -> ParsedDocument:
    if len(source_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_sha256):
        raise CanonicalError("invalid_source_hash")
    objects: dict[str, dict[str, Any]] = {}
    for group in ("texts", "tables", "groups", "pictures"):
        for item in data.get(group, []):
            ref = item.get("self_ref")
            if not isinstance(ref, str) or ref in objects:
                raise CanonicalError("invalid_element_reference")
            if not ref.startswith(f"#/{group}/"):
                raise CanonicalError("invalid_element_reference")
            objects[ref] = item
            if len(objects) > 100000:
                raise CanonicalError("element_limit")
    body = data.get("body", {})
    blocks: list[SourceBlock] = []
    visited: set[str] = set()
    warnings: set[str] = set()
    for warning in ("native_text_order_uncertain", "png_dpi_assumed"):
        if warning in data.get("partner_quality_warnings", []):
            warnings.add(warning)

    def visit(ref: str, depth: int = 0) -> None:
        if depth > 128:
            raise CanonicalError("element_depth_limit")
        if ref in visited:
            raise CanonicalError("repeated_element_reference")
        visited.add(ref)
        item = objects.get(ref)
        if item is None:
            raise CanonicalError("missing_element_reference")
        table = item.get("data", {})
        raw_cells = table.get("table_cells", [])
        if len(raw_cells) > 10000:
            raise CanonicalError("table_cell_limit")
        cells = tuple(
            sorted(
                (
                    TableCell(
                        row=c["start_row_offset_idx"],
                        column=c["start_col_offset_idx"],
                        row_span=c.get("row_span", 1),
                        column_span=c.get("col_span", 1),
                        column_header=c.get("column_header", False),
                        row_header=c.get("row_header", False),
                        text=c.get("text", ""),
                    )
                    for c in raw_cells
                ),
                key=lambda cell: (cell.row, cell.column),
            )
        )
        occupied: set[tuple[int, int]] = set()
        for cell in cells:
            end_row, end_column = cell.row + cell.row_span, cell.column + cell.column_span
            if end_row > table.get("num_rows", 10000) or end_column > table.get("num_cols", 10000):
                raise CanonicalError("table_cell_out_of_bounds")
            if cell.row_span * cell.column_span > 10000:
                raise CanonicalError("table_span_limit")
            for row in range(cell.row, end_row):
                for column in range(cell.column, end_column):
                    if (row, column) in occupied:
                        raise CanonicalError("overlapping_table_cells")
                    occupied.add((row, column))
                    if len(occupied) > 100000:
                        raise CanonicalError("table_grid_limit")
        text = item.get("text", "")
        if cells:
            # Expand vertical spans per row without duplicating horizontally merged text.
            headers = [c.text for c in cells if c.column_header]
            row_cells: dict[int, list[TableCell]] = {}
            for cell in cells:
                if cell.column_header:
                    continue
                for row in range(cell.row, cell.row + cell.row_span):
                    row_cells.setdefault(row, []).append(cell)
            text = "\n".join(
                " | ".join(
                    headers
                    + [cell.text for cell in sorted(row_cells[row], key=lambda value: value.column)]
                )
                for row in sorted(row_cells)
            ) or " | ".join(headers)
        if text:
            positions = []
            for prov in item.get("prov", []):
                page = prov["page_no"]
                dimensions = data.get("pages", {}).get(str(page), {}).get("size", {})
                bbox = prov.get("bbox")
                positions.append(
                    SourcePosition(
                        element_path=ref,
                        page=page,
                        bbox=(bbox["l"], bbox["t"], bbox["r"], bbox["b"]) if bbox else None,
                        origin=bbox.get("coord_origin") if bbox else None,
                        page_width=dimensions.get("width"),
                        page_height=dimensions.get("height"),
                    )
                )
            if not positions:
                positions = [SourcePosition(element_path=ref)]
                warnings.add("element_locator_without_page_bbox")
            digest = hashlib.sha256(text.encode()).hexdigest()
            related = tuple(
                link["$ref"]
                for key in ("captions", "footnotes", "references")
                for link in item.get(key, [])
            )
            if any(path not in objects for path in related):
                raise CanonicalError("missing_related_reference")
            blocks.append(
                SourceBlock(
                    id=uuid5(version_id, f"{ref}:{digest}"),
                    ordinal=len(blocks),
                    kind=item.get("label", "paragraph"),
                    text=text,
                    text_sha256=digest,
                    positions=tuple(positions),
                    cells=cells,
                    related_paths=related,
                )
            )
        for child in item.get("children", []):
            visit(child["$ref"], depth + 1)

    for child in body.get("children", []):
        visit(child["$ref"])
    if not blocks:
        raise CanonicalError("empty_document")
    return ParsedDocument(
        document_id=document_id,
        version_id=version_id,
        source_sha256=source_sha256,
        parser_version=parser_version,
        blocks=tuple(blocks),
        warnings=tuple(sorted(warnings)),
        footnote_relationships=tuple(
            FootnoteRelationship.model_validate(item)
            for item in data.get("partner_footnote_relationships", [])
        ),
    )


def assert_reviewable(document: ParsedDocument) -> None:
    """Quality uncertainty blocks automatic readiness; a curator must resolve it."""
    if "native_text_order_uncertain" in document.warnings:
        raise CanonicalError("manual_quality_review_required")
    footnotes = [block for block in document.blocks if block.kind == "footnote"]
    linked = {link.text for link in document.footnote_relationships}
    related = {path for block in document.blocks for path in block.related_paths}
    if any(
        block.text not in linked
        and not any(position.element_path in related for position in block.positions)
        for block in footnotes
    ):
        raise CanonicalError("unlinked_footnote_review_required")


def approved_context(
    document: ParsedDocument, approved_block_ids: frozenset[UUID]
) -> tuple[SourceBlock, ...]:
    """Keep source-linked notes only when explicitly approved in the same disclosure set."""
    by_id = {block.id: block for block in document.blocks}
    if not approved_block_ids <= by_id.keys():
        raise CanonicalError("unknown_approved_block")
    approved = tuple(block for block in document.blocks if block.id in approved_block_ids)
    paths = {position.element_path for block in approved for position in block.positions}
    for block in approved:
        if any(path not in paths for path in block.related_paths):
            # Do not silently append a potentially restricted note or omit a qualification.
            raise CanonicalError("incomplete_approved_context")
    parent_ids: dict[str, set[UUID]] = {}
    for block in document.blocks:
        for path in block.related_paths:
            parent_ids.setdefault(path, set()).add(block.id)
    for block in approved:
        if block.kind != "footnote":
            continue
        parents = parent_ids.get(block.positions[0].element_path, set())
        if parents and not parents.intersection(approved_block_ids):
            raise CanonicalError("orphaned_approved_footnote")
        if not parents and document.footnote_relationships and not any(
            item.kind != "footnote" for item in approved
        ):
            raise CanonicalError("orphaned_approved_footnote")
    if document.footnote_relationships:
        # Paragraph ordinals are OOXML coordinates, not Docling list indices. Without
        # an exact paragraph mapping, the conservative review boundary is the document.
        approved_notes = {block.text for block in approved if block.kind == "footnote"}
        if any(note.text not in approved_notes for note in document.footnote_relationships):
            raise CanonicalError("incomplete_approved_footnotes")
    return approved


def review_context(
    document: ParsedDocument,
) -> tuple[
    dict[UUID, tuple[int, ...]], dict[UUID, tuple[int, ...]], tuple[int, ...], bool, str | None
]:
    """Explain required context without selecting it on behalf of a curator."""
    by_path = {block.positions[0].element_path: block.ordinal for block in document.blocks}
    related = {
        block.id: tuple(sorted({by_path[path] for path in block.related_paths}))
        for block in document.blocks if block.related_paths
    }
    parents_by_path: dict[str, list[int]] = {}
    for block in document.blocks:
        for path in block.related_paths:
            parents_by_path.setdefault(path, []).append(block.ordinal)
    parents = {
        block.id: tuple(sorted(set(parents_by_path.get(block.positions[0].element_path, []))))
        for block in document.blocks if block.kind == "footnote"
    }
    note_by_text: dict[str, int] = {}
    for block in document.blocks:
        if block.kind == "footnote":
            note_by_text.setdefault(block.text, block.ordinal)
    note_ordinals: list[int] = []
    unresolved = False
    for note in document.footnote_relationships:
        ordinal = note_by_text.get(note.text)
        if ordinal is not None:
            note_ordinals.append(ordinal)
        else:
            unresolved = True
    reason: str | None = None
    try:
        assert_reviewable(document)
    except CanonicalError as error:
        reason = str(error)
    return related, parents, tuple(sorted(set(note_ordinals))), unresolved, reason
