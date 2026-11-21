"""Other answer -> immutable admission -> owner export, using the loopback fixture only."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

if os.environ.get("PARTNER_DIAGRAM_FIXTURE_CONFIRMED") != "1":
    raise SystemExit("Requires an isolated loopback fixture, never a paid model")
account = json.loads(Path(os.environ["PARTNER_DIAGRAM_BROWSER_ACCOUNT_FILE"]).read_text(
    encoding="utf-8-sig",
))
account = account.get("account", account)
ORIGIN = "http://localhost:8080"

with sync_playwright() as session:
    browser = session.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
    page = browser.new_page(viewport={"width": 1365, "height": 1000})
    page.goto(ORIGIN)
    page.get_by_label("Логин", exact=True).fill(account["login"])
    page.get_by_label("Пароль", exact=True).fill(account["password"])
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.locator("button.new-chat")).to_be_enabled(timeout=20000)
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url.endswith("/api/v1/chats")) as creation:
        page.locator("button.new-chat").click()
    chat = creation.value.json()
    base = ORIGIN + f"/api/v1/chats/{chat['id']}"
    try:
        page.get_by_role("textbox", name="Сообщение", exact=True).fill(
            "Синтетический неизвестный протокол: уточни обмен журналами."
        )
        page.get_by_role("button", name="Отправить сообщение", exact=True).click()
        card = page.get_by_role("region", name="Проект Синоникса · версия 1", exact=True)
        choices = card.get_by_role("region", name="Уточнения проекта", exact=True)
        other = choices.get_by_role("button", name="Другое", exact=True)
        expect(other).to_be_enabled(timeout=30000)
        other.click()
        original = "Нужен собственный формат CSV <&> с исходными именами."
        choices.get_by_role("textbox", name="Свой ответ", exact=True).fill(original)
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and response.url.endswith("/runs")) as submitted:
            choices.get_by_role("button", name="Применить ответы (1)", exact=True).click()
        assert submitted.value.status == 202
        command = submitted.value.request.post_data_json["project_answer"]
        assert command["remarks"][0]["text"] == original
        assert submitted.value.json()["project_answer_remarks"][0]["text"] == original
        payload = page.request.get(base + "/vendor-wishes/export").json()
        assert payload["wishes"][0]["text"] == original
        assert payload["wishes"][0]["origin"] == "user_submission"
        expect(page.get_by_text("Готово", exact=True)).to_have_count(2, timeout=30000)
        page.get_by_role("button", name="Пожелания и отзывы", exact=True).click()
        page.get_by_text("Пожелания вендору", exact=True).click()
        with page.expect_download() as download:
            page.get_by_role("button", name="Скачать пожелания JSON", exact=True).click()
        saved = json.loads(download.value.path().read_text())
        assert saved == payload
        page.reload()
        expect(page.locator("button.new-chat")).to_be_enabled(timeout=20000)
        assert page.request.get(base + "/vendor-wishes/export").json() == payload
        assert page.request.get(base + "/accounting").json()["summary"]["calls_count"] == 1
    finally:
        token = page.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
        assert page.request.delete(base, headers={
            "Origin": ORIGIN, "X-CSRF-Token": token,
        }).status == 204
        browser.close()
print("Other text saved with zero extra provider calls; owner JSON, download and reload verified")
