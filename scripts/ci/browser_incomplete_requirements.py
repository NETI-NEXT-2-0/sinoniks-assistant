"""Controlled UI/runtime N01/N05/U04 paths; fixed fixtures do not evaluate model extraction."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

if os.environ.get("PARTNER_DIAGRAM_FIXTURE_CONFIRMED") != "1":
    raise SystemExit("Requires the isolated loopback fixture, never a paid endpoint")
account = json.loads(Path(os.environ["PARTNER_DIAGRAM_BROWSER_ACCOUNT_FILE"]).read_text(
    encoding="utf-8-sig",
))
account = account.get("account", account)
ORIGIN = "http://localhost:8080"

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
    created = []

    def start():
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and response.url.endswith("/api/v1/chats")) as creation:
            page.locator("button.new-chat").click()
        assert creation.value.status == 201
        chat = creation.value.json()
        created.append(chat["id"])
        return chat

    def send(text, complete):
        page.get_by_role("textbox", name="Сообщение", exact=True).fill(text)
        page.get_by_role("button", name="Отправить сообщение", exact=True).click()
        expect(page.get_by_text("Готово", exact=True)).to_have_count(complete, timeout=60000)

    def latest(chat):
        return page.request.get(ORIGIN + f"/api/v1/chats/{chat['id']}/designs/latest",
                                params={"branch_id": chat["default_branch_id"]}).json()["design"]

    def ledger(chat):
        return page.request.get(ORIGIN + f"/api/v1/chats/{chat['id']}/accounting").json()

    try:
        chat = start()
        send("Синтетические две открытые сети: офис и цех обмениваются журналами.", 1)
        first = latest(chat)
        assert first["result"]["synonyx"] is None
        card = page.get_by_role("region", name="Проект Синоникса · версия 1", exact=True)
        card.get_by_role("button", name="Не знаю", exact=True).click()
        card.get_by_role("button", name="Применить ответы (1)", exact=True).click()
        expect(page.get_by_text("Готово", exact=True)).to_have_count(2, timeout=30000)
        unknown = latest(chat)
        assert unknown["input"]["unknowns"][0]["path"] == "/protected_network_id"
        assert ledger(chat)["summary"]["calls_count"] == 1
        page.get_by_role("button", name="Проект Синоникса", exact=True).click()
        panel = page.get_by_role("dialog")
        select = panel.get_by_role("combobox", name="Считать защищаемой", exact=True)
        select.select_option(label="Цех")
        panel.get_by_role("button", name="Сохранить требования", exact=True).click()
        expect(panel.get_by_text("Сохранена версия 3.", exact=False)).to_be_visible()
        chosen = latest(chat)
        protected = next(n for n in chosen["input"]["networks"] if n["name"] == "Цех")
        assert chosen["input"]["protected_network_id"] == protected["id"]
        assert all(n["trust"] == "open" for n in chosen["input"]["networks"])
        assert chosen["result"]["confirmed_rules_count"] == 1
        assert any(a.get("kind") == "protected_network_choice"
                   for a in chosen["result"]["assumptions"])
        assert ledger(chat)["summary"]["calls_count"] == 1
        page.reload()
        page.get_by_role("button", name="Проект Синоникса", exact=True).click()
        panel = page.get_by_role("dialog")
        expect(panel.get_by_role("combobox", name="Считать защищаемой", exact=True)
               ).to_have_value(protected["id"])
        for field in panel.get_by_role("combobox", name="Доверие сети", exact=True).all():
            expect(field).to_have_value("open")
        panel.get_by_role("button", name="К чату", exact=True).click()

        chat = start()
        send("Синтетические три сети: построй общую схему для трёх сетей.", 1)
        assert latest(chat) is None
        expect(page.locator(".assistant-message .prose").last).to_contain_text("Какую пару")
        assert ledger(chat)["summary"]["calls_count"] == 1

        chat = start()
        send("Синтетический N01: двусторонний файловый обмен; параметры неизвестны.", 1)
        partial = latest(chat)
        assert partial["result"]["confirmed_rules_count"] is None
        assert partial["result"]["file_users_count"] is None
        assert all(side["checks"] is None for side in partial["input"]["flows"][0]["sides"])
        expect(page.get_by_role("region", name="Уточнения проекта", exact=True)).to_be_visible()

        chat = start()
        send("Синтетический N05: журналы UDP 514 наружу; обновления внутрь пока без параметров.", 1)
        partial = latest(chat)
        assert partial["result"]["confirmed_rules_count"] is None
        send("Синтетический N05 подробности: один сотрудник, USB, SMB/PUSH, внешний ICAP.", 2)
        final = latest(chat)
        assert final["result"]["confirmed_rules_count"] == 2
        assert final["result"]["file_users_count"] == 1
        assert ledger(chat)["summary"]["calls_count"] == 2
        assert any(flow["kind"] == "data" for flow in final["input"]["flows"])

        chat = start()
        send("Синтетический смешанный: журналы UDP 514. Нужен встроенный антивирус", 1)
        mixed = latest(chat)
        assert mixed["result"]["confirmed_rules_count"] == 1
        assert "Нужен встроенный антивирус" in mixed["result"]["requests_for_vendor"]
        assert not errors, errors
    finally:
        token = page.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
        for chat_id in created:
            assert page.request.delete(ORIGIN + f"/api/v1/chats/{chat_id}", headers={
                "Origin": ORIGIN, "X-CSRF-Token": token,
            }).status == 204
        browser.close()
print("N01/N05/U04, durable open trust/unknown/choice and mixed request UI paths verified")
