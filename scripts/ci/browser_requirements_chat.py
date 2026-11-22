"""Actual chat/HTTP/worker/ledger path with explicit loopback fixture only."""

import json
import os
import uuid
from pathlib import Path
from xml.etree.ElementTree import fromstring

from playwright.sync_api import expect, sync_playwright

if os.environ.get("PARTNER_DIAGRAM_FIXTURE_CONFIRMED") != "1":
    raise SystemExit("Requires the isolated loopback fixture, never a paid model")
account = json.loads(
    Path(os.environ["PARTNER_DIAGRAM_BROWSER_ACCOUNT_FILE"]).read_text(encoding="utf-8-sig")
)
ORIGIN = "http://localhost:8080"
PRIVATE = Path(__file__).resolve().parents[2] / ".local/ci-private"
PRIVATE.mkdir(parents=True, exist_ok=True)
with sync_playwright() as session:
    browser = session.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
    page = browser.new_page(viewport={"width": 1365, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(ORIGIN)
    page.get_by_label("Логин", exact=True).fill(account["login"])
    page.get_by_label("Пароль", exact=True).fill(account["password"])
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.locator("button.new-chat")).to_be_enabled(timeout=20000)

    def headers():
        token = page.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
        return {"Origin": ORIGIN, "X-CSRF-Token": token}

    response = page.request.post(
        ORIGIN + "/api/v1/chats", headers=headers(), data={"title": "Requirements HTTP fixture"}
    )
    assert response.status == 201
    chat = response.json()
    base = ORIGIN + f"/api/v1/chats/{chat['id']}/designs"
    body = {
        "schema_version": 3,
        "id": "synthetic-project",
        "networks": [
            {"id": "office", "name": "Офис", "trust": None},
            {"id": "plant", "name": "Цех", "trust": "closed"},
        ],
        "systems": [
            {"id": "source", "name": "Источник", "network_id": "plant"},
            {"id": "target", "name": "Приёмник", "network_id": "office"},
        ],
        "flows": [
            {
                "id": "logs",
                "kind": "data",
                "sender_network_id": "plant",
                "receiver_network_id": "office",
                "sender_ids": ["source"],
                "receiver_ids": ["target"],
                "purpose": "Журналы",
                "mode": "one-way",
                "protocol": None,
                "destination_port": 514,
            }
        ],
    }
    fresh = os.environ.get("PARTNER_NEW_PROJECT_FIXTURE") == "1"
    first = None
    if not fresh:
        saved = page.request.post(
            base,
            headers={**headers(), "Idempotency-Key": str(uuid.uuid4())},
            data={"branch_id": chat["default_branch_id"], "input": body},
        )
        assert saved.status == 201
        first = saved.json()
    page.reload()
    box = page.get_by_role("textbox", name="Сообщение", exact=True)
    scene_route = "**/designs/*/diagram"
    page.route(scene_route, lambda route: route.fulfill(status=503, json={"code": "temporary"}))
    box.fill(
        "Синтетический новый проект: цех передаёт журналы в открытый офис по UDP 514. "
        if fresh
        else "Синтетическое уточнение: офис открытый, протокол UDP."
    )
    page.get_by_role("button", name="Отправить сообщение", exact=True).click()
    card = page.get_by_role("region", name="Проект Синоникса · версия " + ("1" if fresh else "2"))
    expect(card).to_be_visible(timeout=30000)
    expect(card.get_by_text("Расчёт Синоникса 1.9 сохранён.", exact=False)).to_be_visible()
    retry = card.get_by_role("button", name="Повторить загрузку", exact=True)
    expect(retry).to_be_visible(timeout=30000)
    expect(page.get_by_text("Готово", exact=True)).to_have_count(0)
    page.unroute(scene_route)
    retry.click()
    expect(card.get_by_role("img", name="Схема Синоникса · Правила 1–1")).to_be_visible()
    expect(card.get_by_role("region", name="Правила сохранённой схемы")).to_be_visible()
    expect(page.get_by_text("Готово", exact=True)).to_be_visible(timeout=30000)
    with page.expect_download() as downloaded:
        card.get_by_role("button", name="Схема draw.io", exact=True).click()
    xml = fromstring(downloaded.value.path().read_bytes())
    assert len(json.loads(xml.attrib["data-rule-rows"])) == 1
    latest = page.request.get(base + "/latest?branch_id=" + chat["default_branch_id"]).json()[
        "design"
    ]
    assert (
        latest["parent_id"] == (first["id"] if first else None) and latest["status"] == "completed"
    )
    assert latest["result"]["confirmed_rules_count"] == 1
    assert latest["input"]["flows"][0]["protocol"] == "UDP"
    card.get_by_role("button", name="Открыть проект", exact=True).click()
    panel = page.get_by_role("dialog")
    expect(panel.get_by_role("heading", name="Требования к Синониксу 1.9")).to_be_visible()
    panel.get_by_role("button", name="К чату", exact=True).click()
    box.fill("Синтетическое пояснение сохранённого проекта")
    page.get_by_role("button", name="Отправить сообщение", exact=True).click()
    expect(page.get_by_text("Готово", exact=True)).to_have_count(2, timeout=30000)
    same = page.request.get(base + "/latest?branch_id=" + chat["default_branch_id"]).json()[
        "design"
    ]
    assert same == latest
    ledger = page.request.get(ORIGIN + f"/api/v1/chats/{chat['id']}/accounting").json()
    assert ledger["summary"]["calls_count"] == 2
    assert ledger["mean_full_elapsed_ms"] is not None
    page.screenshot(path=str(PRIVATE / "requirements-chat.png"))
    assert not errors, errors
    assert (
        page.request.delete(ORIGIN + f"/api/v1/chats/{chat['id']}", headers=headers()).status == 204
    )
    browser.close()
print("V3 chat clarification and read-only explanation through HTTP fixture/worker/ledger verified")
