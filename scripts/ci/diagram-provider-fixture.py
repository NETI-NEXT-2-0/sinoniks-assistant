"""Loopback-only HTTP fixture for browser/runtime integration, never model quality.

Run explicitly in an isolated test environment. This is not a product fallback.
"""

import argparse
import json
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def edit_scenario(proposal: dict, question: str) -> tuple[str, str] | None:
    """Reserved U03 markers exercise persistence, not semantic interpretation."""
    if "Синтетический U03" not in question:
        return None
    if "начало" in question:
        flow = proposal["flows"][0]
        proposal["systems"] = [
            {"id": f"sender-{n}", "name": f"Источник {n}",
             "network_id": flow["sender_network_id"]} for n in range(1, 4)
        ] + [{"id": "receiver", "name": "Приёмник", "network_id": flow["receiver_network_id"]}]
        flow["sender_ids"] = [f"sender-{n}" for n in range(1, 4)]
        flow["receiver_ids"] = ["receiver"]
    if "добавить" in question:
        flow = proposal["flows"][0]
        proposal["systems"].append({"id": "receiver-extra", "name": "Резервный приёмник",
                                    "network_id": flow["receiver_network_id"]})
        flow["receiver_ids"].append("receiver-extra")
    if "переименовать" in question:
        next(system for system in proposal["systems"] if system["name"] == "Резервный приёмник")[
            "name"
        ] = "Архив журналов"
    if "неоднозначно удалить" in question:
        return "insufficient", "Какой сервер удалить: Приёмник или Архив журналов?"
    if "удалить Архив журналов" in question:
        chosen = next(system["id"] for system in proposal["systems"]
                      if system["name"] == "Архив журналов")
        proposal["systems"] = [system for system in proposal["systems"] if system["id"] != chosen]
        for flow in proposal["flows"]:
            flow["receiver_ids"] = [identifier for identifier in flow["receiver_ids"]
                                     if identifier != chosen]
    if "открыть подсеть" in question:
        return "insufficient", ("Прямое соединение подсетей не входит в проект обмена. "
                                "Схема сохранена.")
    if "объяснить" in question:
        return "project_explanation", "Три источника отправляют журналы одному приёмнику UDP 514."
    return "design", "Синтетическая правка требований."


def incomplete_scenario(proposal: dict, question: str) -> None:
    """Fixed synthetic cases only; this is not free-text extraction or a product fallback."""
    if "Синтетические две открытые сети" in question:
        for network in proposal["networks"]:
            network["trust"] = "open"
    if "Синтетические три сети" in question:
        proposal["networks"].append({"id": "third", "name": "Третья сеть", "trust": "open"})
    if "Синтетический смешанный" in question:
        proposal["requests_for_vendor"] = [{"id": "wish-mixed", "kind": "unsupported",
                                            "text": "Нужен встроенный антивирус"}]
    if "Синтетический N01" in question or "Синтетический N05" in question:
        outer, inner = (network["id"] for network in proposal["networks"])
        files = next((flow for flow in proposal["flows"] if flow["kind"] == "file"), None)
        if files is None:
            files = {"id": "updates", "kind": "file", "sender_network_id": outer,
                     "receiver_network_id": inner, "sender_ids": [], "receiver_ids": [],
                     "purpose": "Обновления", "direction": "forward",
                     "sides": [{"network_id": outer}, {"network_id": inner}]}
            proposal["flows"].append(files)
        if "Синтетический N01" in question:
            files["direction"] = "both"
            proposal["flows"] = [files]
        if "подробности" in question:
            proposal["systems"].extend([
                {"id": "usb-n05", "name": "USB-обновления", "kind": "usb",
                 "attachment": "node-A", "network_id": None},
                {"id": "store-n05", "name": "Хранилище", "kind": "storage", "network_id": inner},
            ])
            proposal["actors"] = [{"id": "operator-n05", "name": "Сотрудник"}]
            proposal["manual_users"] = {"lower": 1, "upper": 1}
            files.update(sender_ids=["usb-n05"], receiver_ids=["store-n05"], sides=[
                {"network_id": outer, "manual": False, "usb": True,
                 "actor_ids": ["operator-n05"], "received_files_shared": True,
                 "checks": [{"kind": "icap", "icap_service": "antivirus"}]},
                {"network_id": inner, "manual": False, "usb": False, "checks": [],
                 "received_files_shared": True,
                 "automations": [{"operation": "PUSH", "protocol": "SMB",
                                  "storage_id": "store-n05"}]},
            ])


def fixture_body(messages: list[dict] | None = None) -> dict:
    if messages and "Синтетическая справка" in messages[-1]["content"].split("Evidence:", 1)[0]:
        identifier = re.search(r"\[(source_[0-9]+)\]", messages[-1]["content"])
        assert identifier is not None
        result = {"mode": "grounded", "answer": "Контрольный факт: не более 256 правил.",
                  "citations": [{"evidence_id": identifier[1],
                                 "quote": "Контрольный факт: не более 256 правил."}]}
        return {"id": "synthetic-reference-" + uuid.uuid4().hex,
                "choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    flow = {
        "sender_side": "A",
        "sender": "App1",
        "receiver": "Приёмник",
        "mode": "diode",
        "service": "Журналы",
        "policy": "Только журналы",
        "protocol": "UDP",
        "destination_port": 514,
    }
    proposal = {
        "schema_version": 2,
        "network_a": "Офис",
        "network_b": "Производство",
        "firewall": "A",
        "exchanges": [flow | {"sender": name} for name in ("App1", "App2", "App3")],
    }
    requirements = False
    if messages and "REQUIREMENTS v3" in messages[0].get("content", ""):
        label = (
            "New draft requirements (not yet saved)"
            if "UNSAVED NEW PROJECT" in messages[0]["content"]
            else "Saved project conditions"
        )
        context = messages[-1]["content"].split("\n" + label + ":\n", 1)[-1]
        proposal = json.loads(context)
        requirements = True
        if "UNSAVED NEW PROJECT" in messages[0]["content"]:
            proposal["networks"] = [
                {"id": "office", "name": "Офис", "trust": "open"},
                {"id": "plant", "name": "Цех", "trust": "closed"},
            ]
            proposal["systems"] = [
                {"id": "sender", "name": "Источник", "network_id": "plant"},
                {"id": "receiver", "name": "Приёмник", "network_id": "office"},
            ]
            proposal["flows"] = [
                {
                    "id": "logs",
                    "kind": "data",
                    "sender_network_id": "plant",
                    "receiver_network_id": "office",
                    "sender_ids": ["sender"],
                    "receiver_ids": ["receiver"],
                    "mode": "one-way",
                    "protocol": "UDP",
                    "destination_port": 514,
                    "purpose": "Журналы",
                }
            ]
        if not all(network["trust"] == "open" for network in proposal["networks"]):
            for network in proposal["networks"]:
                network["trust"] = "open" if network["name"] == "Офис" else "closed"
        for exchange in proposal["flows"]:
            if exchange["kind"] == "data":
                exchange["protocol"] = (
                    None
                    if "Синтетический неизвестный протокол"
                    in messages[-1]["content"].split("Evidence:", 1)[0]
                    else "UDP"
                )
        incomplete_scenario(proposal, messages[-1]["content"].split("Evidence:", 1)[0])
    mode = (
        "project_explanation"
        if requirements
        and "Синтетическое пояснение" in messages[-1]["content"].split("Evidence:", 1)[0]
        else "design"
    )
    answer = {
        "mode": mode,
        "design": proposal if mode == "design" else None,
        "citations": [],
        "answer": "Синтетический проект для проверки интерфейса и таймера. " * 12,
    }
    edit = edit_scenario(proposal, messages[-1]["content"].split("Evidence:", 1)[0]) \
        if requirements else None
    if edit:
        mode, text = edit
        answer.update(mode=mode, answer=text, design=proposal if mode == "design" else None)
    return {
        "id": "synthetic-" + uuid.uuid4().hex,
        "model": "synthetic-diagram",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(answer, ensure_ascii=False),
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 120, "cost": 0},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass  # No credentials, prompts or transport details in the test server logs.

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 256000 or not self.path.endswith("/chat/completions"):
            self.send_error(400)
            return
        try:
            payload = json.loads(self.rfile.read(size))
            result = fixture_body(payload.get("messages"))
        except (ValueError, KeyError, TypeError):
            self.send_error(400)
            return
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9099)
    options = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", options.port), Handler).serve_forever()
