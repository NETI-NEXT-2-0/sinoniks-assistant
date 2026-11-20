"""Loopback-only HTTP fixture for browser/runtime integration, never model quality.

Run explicitly in an isolated test environment. This is not a product fallback.
"""

import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def fixture_body(messages: list[dict] | None = None) -> dict:
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
