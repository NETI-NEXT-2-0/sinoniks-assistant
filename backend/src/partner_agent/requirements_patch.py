"""Atomic, entity-addressed edits of a server-owned requirements snapshot.

The model proposes operations. It never selects the stored base revision or
provides provenance. Ownership, revision fencing and persistence stay with the
existing run completion path.
"""

from copy import deepcopy
from typing import Any, Literal

from pydantic import Field

from partner_agent.requirements_input import (
    bind_usb_source,
    normalize_file_side_actor_ids,
    normalize_note_entity_paths,
)
from partner_agent.requirements_service import without_sources
from partner_agent.sinoniks_contract import Identifier, StrictModel
from partner_agent.sinoniks_contract19 import Requirements19, pointer_value

COLLECTIONS = ("networks", "systems", "flows", "actors", "requests_for_vendor")
ROOT_FIELDS = {"manual_users", "protected_network_id", "existing_firewall_name",
               "unknowns", "assumptions"}


class PatchError(ValueError):
    pass


class EntityEdit(StrictModel):
    op: Literal["add", "update", "remove"]
    collection: Literal["networks", "systems", "flows", "actors", "requests_for_vendor", "project"]
    id: Identifier | None = None
    values: dict[str, Any] = Field(default_factory=dict, max_length=24)


class RequirementsPatch(StrictModel):
    operations: list[EntityEdit] = Field(min_length=1, max_length=64)


def normalize_optional_text(value: Any) -> Any:
    """A blank nullable label denotes an unknown, never an invented value.

    Identifiers, enum values, required explanations and numeric fields remain
    strict. Imported immutable metadata is not rewritten.
    """
    if isinstance(value, list):
        return [normalize_optional_text(item) for item in value]
    if isinstance(value, dict):
        return {key: item if key == "legacy" else None
                if key in {"name", "purpose", "policy", "details", "existing_firewall_name"}
                and isinstance(item, str) and not item.strip()
                else normalize_optional_text(item) for key, item in value.items()}
    return value


def compact_project(document: dict[str, Any]) -> dict[str, Any]:
    """Lossless model view: omit default values and immutable import metadata.

    The original, including defaults and metadata, is retained server-side.
    Null/empty values are not removed generically (e.g. checks=[] means none,
    checks=null means unknown). Pydantic knows each field's actual default.
    """
    if document.get("schema_version") != 3:
        return dict(without_sources(document))
    model = Requirements19.model_validate(document)
    view = without_sources(model.model_dump(mode="json", exclude_defaults=True,
                                          exclude={"legacy"}))
    return {**view, "schema_version": 3, "product_version": "1.9"}


def apply_patch(base: dict[str, Any], proposal: Any) -> Requirements19:
    patch = RequirementsPatch.model_validate(normalize_optional_text(proposal))
    if without_sources(proposal) != proposal:
        raise PatchError("Model provenance is not accepted")
    original = Requirements19.model_validate(base).model_dump(mode="json")
    result = deepcopy(original)
    touched: set[tuple[str, str | None]] = set()
    changed: dict[str, set[str]] = {}
    replaced_notes: set[str] = set()
    changed_roots: set[str] = set()
    for edit in patch.operations:
        key = (edit.collection, edit.id)
        if key in touched:
            raise PatchError("Combine edits of the same entity")
        touched.add(key)
        if edit.collection == "project":
            if (edit.op != "update" or edit.id is not None or not edit.values
                    or not edit.values.keys() <= ROOT_FIELDS):
                raise PatchError("Invalid root edit")
            result.update(deepcopy(edit.values))
            changed_roots.update(edit.values)
            replaced_notes.update(edit.values.keys() & {"unknowns", "assumptions"})
            continue
        if edit.id is None or "id" in edit.values:
            raise PatchError("Address entities by their operation ID")
        rows = result[edit.collection]
        index = next((n for n, row in enumerate(rows) if row["id"] == edit.id), None)
        if edit.op == "add":
            if any(row["id"] == edit.id for name in COLLECTIONS for row in result[name]):
                raise PatchError("New ID already exists")
            rows.append({"id": edit.id, **deepcopy(edit.values)})
        else:
            if index is None:
                raise PatchError("Unknown entity ID")
            if edit.op == "remove":
                if edit.values:
                    raise PatchError("Removal takes no values")
                rows.pop(index)
            else:
                if not edit.values or ("kind" in edit.values
                                      and edit.values["kind"] != rows[index].get("kind")):
                    raise PatchError("Invalid update or change of entity kind")
                rows[index].update(deepcopy(edit.values))
                changed[edit.id] = set(edit.values)
    # Retarget pre-existing pointer notes after an entity was removed. Do not
    # retarget explicitly supplied replacement notes: they address the new state.
    for name in ("unknowns", "assumptions"):
        if name in replaced_notes:
            continue
        notes = []
        for note in original[name]:
            note = deepcopy(note)
            parts = note["path"].split("/")
            if name == "assumptions" and len(parts) == 2 and parts[1] in changed_roots:
                continue
            if len(parts) >= 3 and parts[1] in ("networks", "systems", "flows"):
                rows = result[parts[1]]
                index = next((n for n, row in enumerate(rows)
                              if row["id"] == note["target_id"]), None)
                if index is None:
                    continue
                parts[2] = str(index)
                note["path"] = "/".join(parts)
                if (name == "assumptions" and len(parts) > 3
                        and parts[3] in changed.get(note["target_id"], set())):
                    continue
            if name == "unknowns":
                try:
                    if pointer_value(result, note["path"]) not in (None, []):
                        continue
                except ValueError:
                    continue
            notes.append(note)
        result[name] = notes
    # References, types, network membership, USB and all domain invariants are
    # checked together. No partial mutation escapes on any failed operation.
    bind_usb_source(result)
    normalize_note_entity_paths(result)
    normalize_file_side_actor_ids(result)
    return Requirements19.model_validate(result)
