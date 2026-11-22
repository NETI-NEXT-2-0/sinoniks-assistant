"""Seven U03 turns through a local HTTP fixture; never a live-model quality score."""

import json
import os
from pathlib import Path
from xml.etree.ElementTree import fromstring

from playwright.sync_api import expect, sync_playwright

if os.environ.get("PARTNER_DIAGRAM_FIXTURE_CONFIRMED") != "1":
    raise SystemExit("Requires an isolated loopback fixture")
account = json.loads(Path(os.environ["PARTNER_DIAGRAM_BROWSER_ACCOUNT_FILE"]).read_text(
    encoding="utf-8-sig",
))
ORIGIN = "http://localhost:8080"
steps = [
    ("начало: три источника журналов UDP 514 одному приёмнику", 1, 1),
    ("объяснить причину объединения и получателя", 1, 1),
    ("добавить второй приёмник журналов", 2, 2),
    ("переименовать резервный приёмник в Архив журналов", 2, 3),
    ("неоднозначно удалить один сервер", 2, 3),
    ("удалить Архив журналов", 1, 4),
    ("открыть подсеть целиком", 1, 4),
]

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    state = {}

    def observe(request):
        if request.url.startswith(ORIGIN + "/api/"):
            token = request.headers.get("x-csrf-token")
            if token:
                state["csrf"] = token

    page.on("request", observe)
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    page.goto(ORIGIN)
    page.get_by_label("Логин", exact=True).fill(account["login"])
    page.get_by_label("Пароль", exact=True).fill(account["password"])
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.locator("button.new-chat")).to_be_enabled(timeout=20000)
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url.endswith("/api/v1/chats")) as created:
        page.locator("button.new-chat").click()
    assert created.value.status == 201
    chat = created.value.json()
    base = ORIGIN + f"/api/v1/chats/{chat['id']}"
    original = None
    last = None
    last_xml = None

    def headers():
        # Reuse the UI's token. Fetching /csrf here rotates it behind AuthClient's back.
        return {"Origin": ORIGIN, "X-CSRF-Token": state["csrf"]}

    try:
        for turn, (question, count, revision) in enumerate(steps, 1):
            page.get_by_role("textbox", name="Сообщение", exact=True).fill(
                "Синтетический U03 " + question,
            )
            page.get_by_role("button", name="Отправить сообщение", exact=True).click()
            expect(page.get_by_text("Готово", exact=True)).to_have_count(turn, timeout=60000)
            design = page.request.get(base + "/designs/latest", params={
                "branch_id": chat["default_branch_id"],
            }).json()["design"]
            assert design["revision"] == revision
            assert design["result"]["confirmed_rules_count"] == count
            if turn in (2, 5, 7):
                assert design == last, "Read-only turn created a revision"
            if turn == 5:
                expect(page.locator(".assistant-message .prose").last).to_contain_text(
                    "Какой сервер удалить",
                )
            if turn == 7:
                expect(page.locator(".assistant-message .prose").last).to_contain_text(
                    "не входит в проект",
                )
            page.get_by_role("button", name="Проект Синоникса", exact=True).click()
            panel = page.get_by_role("dialog")
            svg = panel.locator(".diagram-viewer svg")
            expect(svg).to_be_visible(timeout=20000)
            with page.expect_download() as native:
                panel.get_by_role("button", name="Схема draw.io", exact=True).click()
            xml = native.value.path().read_bytes()
            assert len(json.loads(fromstring(xml).attrib["data-rule-rows"])) == count
            if turn in (2, 5, 7):
                assert xml == last_xml
            if turn == 4:
                assert "Архив журналов" in xml.decode()
            panel.get_by_role("button", name="К чату", exact=True).click()
            ledger = page.request.get(base + "/accounting").json()
            assert ledger["summary"]["calls_count"] == turn
            if original is None:
                original = (design["id"], xml)
            archived = page.request.post(
                base + f"/designs/{original[0]}/download/drawio", headers=headers(),
            )
            assert archived.status == 200 and archived.body() == original[1]
            last, last_xml = design, xml
        page.reload()
        expect(page.get_by_text("Готово", exact=True)).to_have_count(7, timeout=20000)
        restored = page.request.get(base + "/designs/latest", params={
            "branch_id": chat["default_branch_id"],
        }).json()["design"]
        assert restored == last
        assert len(page.request.get(base + "/accounting").json()["turns"]) == 7
        assert not errors, errors
    finally:
        assert page.request.delete(base, headers=headers()).status == 204
        browser.close()
print("U03: seven turns, counts 1→2→2→1, read-only steps, exports and reload passed")
