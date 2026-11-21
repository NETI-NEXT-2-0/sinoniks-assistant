"""Revision-bound field answers; this is not a general JSON patch interpreter."""

import json
import re
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import Field, JsonValue, ValidationError, model_validator

from partner_agent.infra.errors import APIError
from partner_agent.requirements_service import requirements_questions
from partner_agent.sinoniks_contract import Identifier, StrictModel
from partner_agent.sinoniks_contract19 import Requirements19, pointer_value

INDEX = r"(?:0|[1-9][0-9]{0,2})"
PATH = re.compile(
    rf"^/(?:networks/{INDEX}/(?:name|trust)|systems/{INDEX}/(?:name|network_id)|"
    rf"flows/{INDEX}/(?:purpose|policy|mode|protocol|destination_port|sender_network_id|"
    rf"receiver_network_id|direction|sender_ids|receiver_ids|sides/{INDEX}/(?:manual|usb|"
    rf"manual_protocol|checks|received_files_shared|access_group|automations/{INDEX}/"
    r"(?:protocol|storage_id)))|manual_users|protected_network_id)$"
)


class ProjectAnswer19(StrictModel):
    schema_version: Literal[1] = 1
    base_revision_id: UUID
    path: str = Field(min_length=1, max_length=180)
    target_id: Identifier | None = None
    side_network_id: Identifier | None = None
    value: JsonValue = None
    unknown: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def allowed_field(self) -> "ProjectAnswer19":
        if PATH.fullmatch(self.path) is None:
            raise ValueError("This field cannot be answered with a structured command")
        if len(json.dumps(self.value, ensure_ascii=False)) > 8000:
            raise ValueError("Answer exceeds the structured value limit")
        if self.unknown and self.value is not None:
            raise ValueError("An unknown answer has no supplied value")
        root = self.path in ("/manual_users", "/protected_network_id")
        if root != (self.target_id is None):
            raise ValueError("Bind this answer to its stable entity ID")
        if ("/sides/" in self.path) != (self.side_network_id is not None):
            raise ValueError("File-side answers require their network ID")
        return self


def apply_answer(request: Requirements19, answer: ProjectAnswer19) -> Requirements19:
    document = request.model_dump(mode="json")
    if answer.path == "/protected_network_id":
        document.setdefault("protected_network_id", None)
    segments = answer.path.split("/")
    try:
        if answer.target_id is not None:
            entity = pointer_value(document, "/".join(segments[:3]))
            if not isinstance(entity, dict) or entity.get("id") != answer.target_id:
                raise ValueError("Entity mismatch")
        if answer.side_network_id is not None:
            side = pointer_value(document, "/".join(segments[:5]))
            if not isinstance(side, dict) or side.get("network_id") != answer.side_network_id:
                raise ValueError("File side mismatch")
        parent = pointer_value(document, "/".join(segments[:-1])) if len(segments) > 2 else document
        if not isinstance(parent, dict) or segments[-1] not in parent:
            raise ValueError("Field unavailable")
    except ValueError:
        raise APIError(409, "question_context_changed", "Reload the project question") from None
    field = segments[-1]
    empty_value: JsonValue = [] if field in ("sender_ids", "receiver_ids") else None
    if answer.unknown and parent[field] != empty_value:
        raise APIError(409, "question_context_changed", "The project field already has a value")
    if not answer.unknown:
        parent[field] = answer.value
    for collection in ("unknowns", "assumptions"):
        document[collection] = [
            note
            for note in document[collection]
            if note["path"] != answer.path and not note["path"].startswith(answer.path + "/")
        ]
    if answer.unknown:
        document["unknowns"].append(
            {"path": answer.path, "target_id": answer.target_id, "reason": "user_unknown"}
        )
    try:
        return Requirements19.model_validate(document)
    except ValidationError:
        raise APIError(
            422, "invalid_project_answer", "The answer conflicts with project fields"
        ) from None


def question_choices(request: Requirements19) -> list[dict[str, Any]]:
    output = []
    for question in requirements_questions(request):
        if question["answered_unknown"] or question["reason"] not in (
            "missing",
            "conflict",
            "optional",
        ):
            continue
        path = question["field"]
        field = path.split("/")[-1]
        choices: list[dict[str, Any]] = []
        kind = "unsupported"
        side_network = None
        if PATH.fullmatch(path):
            kind = "text"
            if "/sides/" in path:
                side_network = cast(
                    dict[str, Any],
                    pointer_value(request.model_dump(mode="json"), "/".join(path.split("/")[:5])),
                )["network_id"]
            values: list[tuple[str, Any]] = []
            if field == "trust":
                values = [("Открытая", "open"), ("Закрытая, защищаемая", "closed")]
            elif field == "protected_network_id":
                values = [(f"Считать «{network.name or network.id}» защищаемой", network.id)
                          for network in request.networks]
            elif field == "mode":
                values = [("Только в одну сторону", "one-way"), ("С ответом", "two-way")]
                flow = cast(
                    dict[str, Any],
                    pointer_value(request.model_dump(mode="json"), "/".join(path.split("/")[:3])),
                )
                if flow.get("application") == "ntp":
                    values = [("Только в одну сторону", "one-way")]
                elif flow.get("protocol") in ("MQTT", "Modbus TCP", "МЭК 104"):
                    values = [("С ответом", "two-way")]
            elif field == "direction":
                values = [("В одну сторону", "forward"), ("В обе стороны", "both")]
            elif field == "protocol":
                protocols = (
                    ("SFTP", "FTP", "SMB")
                    if "/automations/" in path
                    else (
                        "TCP",
                        "UDP",
                        "MQTT",
                        "Modbus TCP",
                        "МЭК 104",
                    )
                )
                values = [(value, value) for value in protocols]
            elif field == "manual_protocol":
                values = [("SFTP, вручную", "SFTP")]
            elif field in ("manual", "usb", "received_files_shared"):
                values = [("Да", True), ("Нет", False)]
            elif field in ("network_id", "sender_network_id", "receiver_network_id"):
                values = [
                    (item.name or f"Сеть {n + 1}", item.id)
                    for n, item in enumerate(request.networks)
                ]
            elif field == "storage_id":
                values = [
                    (item.name or "Хранилище без названия", item.id)
                    for item in request.systems
                    if item.kind == "storage" and item.network_id == side_network
                ]
            elif field in ("sender_ids", "receiver_ids"):
                kind = "multiple"
                flow = cast(
                    dict[str, Any],
                    pointer_value(request.model_dump(mode="json"), "/".join(path.split("/")[:3])),
                )
                network = flow[
                    "sender_network_id" if field == "sender_ids" else "receiver_network_id"
                ]
                values = [
                    (item.name or "Система без названия", item.id)
                    for item in request.systems
                    if item.network_id == network and item.kind != "usb"
                ]
            elif field == "destination_port":
                kind = "number"
            elif field == "checks":
                values = [
                    ("Без проверок", []),
                    (
                        "Внешний антивирус по ICAP",
                        [
                            {"kind": "icap", "icap_service": "antivirus"},
                        ],
                    ),
                    ("Внешняя DLP по ICAP", [{"kind": "icap", "icap_service": "dlp"}]),
                    ("Цифровая подпись", [{"kind": "signature"}]),
                ]
            elif field == "manual_users":
                kind = "range"
            if values:
                if kind != "multiple":
                    kind = "choice"
                choices = [{"label": label, "value": value} for label, value in values]
        output.append(
            {**question, "kind": kind, "choices": choices, "side_network_id": side_network}
        )
    return output
