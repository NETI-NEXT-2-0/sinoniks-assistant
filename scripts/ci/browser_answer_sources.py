"""Open a citation, change its permissions and retry through the real source endpoints."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

assert os.environ.get("PARTNER_DIAGRAM_FIXTURE_CONFIRMED") == "1", "Isolated fixture required"
data = json.loads(Path(os.environ["PARTNER_SOURCE_BROWSER_ACCOUNT_FILE"]).read_text())
ORIGIN = "http://localhost:8080"

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    errors = []
    state = {}
    page.on("pageerror", lambda error: errors.append(type(error).__name__))

    def observe(request):
        if request.url.startswith(ORIGIN + "/api/") and request.headers.get("x-csrf-token"):
            state["csrf"] = request.headers["x-csrf-token"]

    page.on("request", observe)
    owner = browser.new_context()
    csrf = owner.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
    login = owner.request.post(ORIGIN + "/api/v1/auth/login", data=data["curator"], headers={
        "Origin": ORIGIN, "X-CSRF-Token": csrf,
    })
    assert login.status == 200
    owner_headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
    revision = 1

    def grant(answer, original):
        global revision
        path = f"/knowledge/documents/{data['document_id']}/grants/{data['reader_id']}"
        response = owner.request.put(
            ORIGIN + "/api/v1" + path,
            headers=owner_headers, data={"expected_revision": revision,
                                         "allow_answer": answer, "allow_original": original},
        )
        assert response.status == 200
        revision = response.json()["revision"]

    page.goto(ORIGIN)
    page.get_by_label("Логин", exact=True).fill(data["reader"]["login"])
    page.get_by_label("Пароль", exact=True).fill(data["reader"]["password"])
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.locator("button.new-chat")).to_be_enabled(timeout=20000)
    with page.expect_response(lambda response: response.url.endswith("/api/v1/chats")
                              and response.request.method == "POST") as created:
        page.locator("button.new-chat").click()
    assert created.value.status == 201
    chat = created.value.json()
    base = ORIGIN + f"/api/v1/chats/{chat['id']}"
    try:
        page.get_by_role("combobox", name="Режим ответа", exact=True).select_option("reference")
        page.get_by_role("textbox", name="Сообщение", exact=True).fill(
            "Синтетическая справка: контрольный факт о 256 правилах",
        )
        page.get_by_role("button", name="Отправить сообщение", exact=True).click()
        expect(page.get_by_text("Готово", exact=True)).to_have_count(1, timeout=60000)
        citation = page.locator(".answer-source").last
        citation.locator("summary").click()
        expect(citation.locator("blockquote")).to_have_text(
            "Контрольный факт: не более 256 правил.",
        )
        citation.get_by_role("button", name="Открыть фрагмент", exact=True).click()
        fragment = citation.get_by_role("region", name="Актуальный фрагмент источника")
        expect(fragment).to_be_visible()
        expect(fragment).to_contain_text("9. Контрольный раздел")
        for width in (320, 390):
            page.set_viewport_size({"width": width, "height": 844})
            expect(fragment).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.set_viewport_size({"width": 1440, "height": 1000})
        assert citation.get_by_role("button", name="Скачать оригинал", exact=True).count() == 0
        grant(True, True)
        citation.get_by_role("button", name="Обновить фрагмент", exact=True).click()
        original = citation.get_by_role("button", name="Скачать оригинал", exact=True)
        expect(original).to_be_visible()
        grant(True, False)
        original.click()
        expect(citation.get_by_role("alert")).to_contain_text("больше недоступен")
        expect(fragment).to_have_count(0)
        citation.get_by_role("button", name="Открыть фрагмент", exact=True).click()
        expect(fragment).to_be_visible()
        citation.locator("summary").click()
        citation.locator("summary").click()
        expect(fragment).to_have_count(0)
        grant(False, False)
        citation.get_by_role("button", name="Открыть фрагмент", exact=True).click()
        expect(citation.get_by_role("alert")).to_contain_text("больше недоступен")
        expect(fragment).to_have_count(0)
        assert page.request.get(base + "/accounting").json()["summary"]["calls_count"] == 1
        assert not errors, errors
    finally:
        assert page.request.delete(base, headers={"Origin": ORIGIN,
                                   "X-CSRF-Token": state["csrf"]}).status == 204
        browser.close()
print("Citation UI: current fragment, original/read revocation, close/reopen and one call passed")
