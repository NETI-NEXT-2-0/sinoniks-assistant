"""Requirements revisions share the existing immutable project history."""

from typing import Any
from uuid import UUID, uuid4

from partner_agent.infra.errors import APIError
from partner_agent.sinoniks_contract import SourceReference
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_contract19_io import assess


def without_sources(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_sources(item) for key, item in value.items() if key != "sources"}
    if isinstance(value, list):
        return [without_sources(item) for item in value]
    return value


def bind_requirements(
    conditions: Requirements19,
    previous: Requirements19 | None,
    *,
    message_id: UUID | None,
    command_id: UUID,
    trusted_import: bool = False,
) -> Requirements19:
    document = conditions.model_dump(mode="json")
    collections = ("networks", "systems", "flows", "actors", "requests_for_vendor")
    previous_objects = {
        item["id"]: item
        for name in collections
        for item in (previous.model_dump(mode="json")[name] if previous else [])
    }
    for name in collections:
        old_ids = {
            item["id"] for item in (previous.model_dump(mode="json")[name] if previous else [])
        }
        if any(
            item["id"] in previous_objects and item["id"] not in old_ids for item in document[name]
        ):
            raise APIError(409, "design_identity_conflict", "Entity IDs cannot change their kind")
    if previous is not None and conditions.legacy != previous.legacy:
        raise APIError(409, "design_identity_conflict", "Keep the original import metadata")
    # The provider may label new objects for references, but the server assigns
    # their durable IDs. Operator-assigned IDs are retained on explicit saves.
    if message_id is not None:
        mapping = {
            item["id"]: "entity-" + uuid4().hex
            for name in collections
            for item in document[name]
            if item["id"] not in previous_objects
        }
        scalar_refs = {
            "id",
            "network_id",
            "sender_network_id",
            "receiver_network_id",
            "storage_id",
            "flow_id",
            "target_id",
            "protected_network_id",
        }
        list_refs = {"sender_ids", "receiver_ids", "actor_ids"}

        def remap(value: Any) -> Any:
            if isinstance(value, list):
                return [remap(item) for item in value]
            if isinstance(value, dict):
                return {
                    key: (
                        mapping.get(item, item)
                        if key in scalar_refs and isinstance(item, str)
                        else [mapping.get(ref, ref) for ref in item]
                        if key in list_refs and isinstance(item, list)
                        else remap(item)
                    )
                    for key, item in value.items()
                }
            return value

        document = remap(document)
        document["id"] = conditions.id if previous is not None else "project-" + uuid4().hex
    source = SourceReference(
        kind="message" if message_id else "operator", reference=str(message_id or command_id)
    ).model_dump(mode="json")
    for name in ("networks", "systems", "flows"):
        for item in document[name]:
            prior = previous_objects.get(item["id"])
            item["sources"] = (
                list(prior.get("sources", []))
                if prior
                else list(item.get("sources", []))
                if trusted_import
                else []
            )
            choice_changed = previous is not None and (
                previous.protected_network_id != document.get("protected_network_id")
            )
            chosen = {document.get("protected_network_id"),
                      previous.protected_network_id if previous else None}
            if (prior is None or without_sources(item) != without_sources(prior)
                or (name == "networks" and choice_changed and item["id"] in chosen)):
                item["sources"] = [*item["sources"], source][-20:]
    return Requirements19.model_validate(document)


def requirements_questions(conditions: Requirements19) -> list[dict[str, Any]]:
    names = {item.id: item.name or f"Сеть {n + 1}"
             for n, item in enumerate(conditions.networks)}
    names.update({item.id: item.name or f"Система {n + 1}"
                  for n, item in enumerate(conditions.systems)})
    names.update({flow.id: flow.purpose or f"Обмен {n + 1}"
                  for n, flow in enumerate(conditions.flows)})
    result = []
    for issue in assess(conditions).issues:
        segments = issue.path.split("/")
        entity = None
        if len(segments) >= 3 and segments[1] in ("networks", "systems", "flows"):
            entity = getattr(conditions, segments[1])[int(segments[2])]
        label = names.get(entity.id, "") if entity else ""
        result.append(
            {
                "field": issue.path,
                "text": f"{label}: {issue.message}" if label else issue.message,
                "reason": issue.kind,
                "answered_unknown": issue.answered_unknown,
                "target_id": entity.id if entity else None,
            }
        )
    return result


def requirements_answer(conditions: Requirements19) -> str:
    status = assess(conditions).status
    pending = [
        item["text"]
        for item in requirements_questions(conditions)
        if item["reason"] in ("missing", "conflict") and not item["answered_unknown"]
    ]
    text = "Сохранил требования Синоникса 1.9."
    if pending:
        text += " Для продолжения уточните:\n\n" + "\n".join(f"• {item}" for item in pending)
    elif status == "preliminary":
        text += " Неизвестные значения и допущения сохранены явно."
    else:
        text += " Основные сведения собраны."
    return text + (
        "\n\nАрхитектура и число правил ещё не рассчитаны. "
        "Требования можно открыть и дополнить в карточке проекта."
    )


def new_project_context(chat_id: UUID) -> dict[str, Any]:
    """Unpersisted extraction scaffold, not a proposed or saved architecture."""
    from partner_agent.sinoniks_contract19 import Network19

    context: dict[str, Any] = without_sources(Requirements19(
        id="project-" + str(chat_id),
        networks=[Network19(id="network-A"), Network19(id="network-B")],
    ).model_dump(mode="json"))
    return context
