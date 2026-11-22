"""Request-local provider grammar; domain validation remains on the server."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import lru_cache
from typing import Any

from partner_agent.sinoniks_contract19 import Requirements19

_schema: ContextVar[dict[str, Any] | None] = ContextVar("model_output_schema", default=None)


@contextmanager
def output_schema_scope(schema: dict[str, Any] | None) -> Iterator[None]:
    token = _schema.set(schema)
    try:
        yield
    finally:
        _schema.reset(token)


def current_output_schema() -> dict[str, Any] | None:
    return _schema.get()


def output_schema_sha256() -> str | None:
    schema = current_output_schema()
    return (hashlib.sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()
            if schema is not None else None)


def strict_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {("anyOf" if key == "oneOf" else key): strict_schema(item)
              for key, item in value.items() if key not in {"default", "discriminator"}}
    if "const" in result:
        result["enum"] = [result.pop("const")]
    if result.get("type") == "object":
        properties = result.get("properties", {})
        for key in ("sources", "legacy"):
            properties.pop(key, None)  # These are assigned only by the server.
        result.update(properties=properties, required=list(properties), additionalProperties=False)
    return result


@lru_cache(maxsize=1)
def requirements_schema() -> dict[str, Any]:
    return strict_schema(Requirements19.model_json_schema())  # type: ignore[no-any-return]


def new_project_output_schema(handles: list[str]) -> dict[str, Any]:
    design = deepcopy(requirements_schema())
    definitions = design.pop("$defs", {})
    # A new file exchange describes both device nodes, including an unknown
    # profile. Omitting a side drops explicitly supplied USB/method facts.
    file_sides = definitions["FileFlow19"]["properties"]["sides"]
    file_sides.update(minItems=2, maxItems=2, description=(
        "One profile for each of the two logical network sides. Include both even if "
        "their methods are unknown (nullable fields); never omit the USB source profile."
    ))
    definitions["FileFlow19"]["properties"]["sender_network_id"]["description"] = (
        "Logical source side. For USB attached to node A use the existing external-side "
        "network ID. This does not put the USB device itself in that network."
    )
    definitions["FileSide19"]["properties"]["actor_ids"]["description"] = (
        "IDs of the stated people manually transferring files on this side, including "
        "a stated contractor bringing updates on USB. Reuse their root actors IDs."
    )
    # Server-owned identity stays out of the model grammar. This also keeps the
    # compiled schema reusable across projects rather than unique for every UUID.
    design["properties"].pop("id")
    design["required"].remove("id")
    definitions["Requirements19"] = design
    # Remove unreachable provenance/legacy definitions, including arbitrary maps.
    reachable: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name not in reachable:
                    reachable.add(name)
                    visit(definitions[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    citation_id: dict[str, Any] = {"type": "string"}
    if handles:
        citation_id["enum"] = handles
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "answer": {"type": "string"},
            "mode": {"type": "string", "enum": [
                "design", "grounded", "insufficient", "conversation"]},
            "citations": {"type": "array", **({"maxItems": 0} if not handles else {}),
                          "items": {"type": "object", "additionalProperties": False,
                                    "properties": {"evidence_id": citation_id},
                                    "required": ["evidence_id"]}},
            "design": {"anyOf": [{"$ref": "#/$defs/Requirements19"}, {"type": "null"}],
                       "description": (
                           "Null for educational product how-to, comparisons and vendor wishes. "
                           "Use an object only for an actual request to design infrastructure "
                           "or capture the user's network exchange conditions."
                       )},
            "vendor_wishes": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"kind": {"type": "string", "enum": ["unsupported", "free_text"]},
                               "text": {"type": "string"}}, "required": ["kind", "text"]}},
        },
        "required": ["answer", "mode", "citations", "design", "vendor_wishes"],
    }
    visit(schema)
    schema["$defs"] = {name: definitions[name] for name in sorted(reachable)}
    return schema
