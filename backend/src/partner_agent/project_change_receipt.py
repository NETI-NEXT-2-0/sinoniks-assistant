"""Factual change receipt from saved requirements, independent of model claims."""

from typing import Any

from partner_agent.requirements_service import without_sources


def change_receipt(
    before: dict[str, Any] | None, after: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    changes: dict[str, Any] = {}
    lines: list[str] = []
    for collection, label in (("systems", "Устройства"), ("flows", "Обмены")):
        old = {row["id"]: row for row in (before or {}).get(collection, [])}
        new = {row["id"]: row for row in after.get(collection, [])}
        added = [row for key, row in new.items() if key not in old]
        updated = [row for key, row in new.items() if key in old
                   and without_sources(row) != without_sources(old[key])]
        removed = [row for key, row in old.items() if key not in new]
        changes[collection] = {"added": [row["id"] for row in added],
                               "updated": [row["id"] for row in updated],
                               "removed": [row["id"] for row in removed]}
        for action, rows in (("добавлены", added), ("изменены", updated), ("удалены", removed)):
            if not rows:
                continue
            names = [str(row.get("name") or row.get("purpose")
                         or row.get("protocol") or row["id"]) for row in rows[:6]]
            if collection == "flows":
                names = [name + (f" ({row['protocol']})" if row.get("protocol")
                                 and row["protocol"] not in name else "")
                         for name, row in zip(names, rows[:6], strict=True)]
            tail = f" и ещё {len(rows) - 6}" if len(rows) > 6 else ""
            lines.append(f"{label} {action}: {', '.join(names)}{tail}.")
    mentioned = {identifier for flow in after.get("flows", [])
                 for identifier in [*flow.get("sender_ids", []), *flow.get("receiver_ids", [])]}
    unconnected = [row for row in after.get("systems", [])
                   if row["id"] in changes["systems"]["added"] and row["id"] not in mentioned]
    changes["new_systems_without_exchange"] = [row["id"] for row in unconnected]
    if before is not None and unconnected:
        unconnected_names = ", ".join(str(row.get("name") or row["id"])
                                      for row in unconnected[:6])
        lines.append(f"Пока не участвуют в сохранённых обменах: {unconnected_names}.")
        if not any(changes["flows"].values()):
            lines.append("Новые обмены не сохранены, прежние не изменились. "
                         "Если требовалось соединение с новым устройством, уточните "
                         "отправителя, получателя и назначение передачи.")
    return changes, "\n".join(lines)
