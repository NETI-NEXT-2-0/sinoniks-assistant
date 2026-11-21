"""Entry and workspace checks on an isolated application fixture."""

import json
import os
import re
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import fromstring

from playwright.sync_api import expect, sync_playwright

assert os.environ.get("PARTNER_ENTRY_FIXTURE_CONFIRMED") == "1"
ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".local/ci-private"
PRIVATE.mkdir(parents=True, exist_ok=True)
account = json.loads(
    Path(os.environ["PARTNER_ENTRY_BROWSER_ACCOUNT_FILE"]).read_bytes()
)
ORIGIN = "http://localhost:8080"


def check_workspace(page, csrf_token):
    chats_url = ORIGIN + "/api/v1/chats"
    assert page.request.get(chats_url).json()["items"] == [], "Use a fresh fixture account"
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token}
    created = []
    composer = page.get_by_role("textbox", name="Сообщение", exact=True)
    example = page.get_by_role("button", name="Подставить пример в сообщение")
    send = page.get_by_role("button", name="Отправить сообщение")

    def post_to(path):
        return lambda response: (
            response.url == chats_url + path and response.request.method == "POST"
        )

    def no_horizontal_overflow():
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    try:
        page.route(re.compile(r"/api/v1/chats/[^/]+/snapshot(?:\?.*)?$"),
                   lambda route: route.fulfill(status=503, json={}), times=1)
        with page.expect_response(post_to("")) as initial_chat:
            example.click()
        assert initial_chat.value.status == 201
        first_id = initial_chat.value.json()["id"]
        created.append(first_id)
        expect(page.get_by_role("alert")).to_be_visible()
        expect(example).to_have_count(0)
        expect(composer).to_be_disabled()
        expect(page.get_by_role("button", name="Повторить загрузку", exact=True)).to_be_visible()
        no_horizontal_overflow()
        page.get_by_role("button", name="Повторить загрузку", exact=True).click()
        expect(composer).to_have_value(re.compile("UDP 514"))
        expect(example).to_be_disabled()
        draft = "Мой черновик: журналы по UDP 514."
        composer.fill(draft)
        assert [chat["id"] for chat in page.request.get(chats_url).json()["items"]] == [first_id]

        page.get_by_role("button", name="Показать список чатов").click()
        with page.expect_response(post_to("")) as new_chat:
            page.locator("button.new-chat").click()
        assert new_chat.value.status == 201
        second_id = new_chat.value.json()["id"]
        created.append(second_id)
        expect(composer).to_have_value("")
        expect(send).to_be_disabled()
        expect(page.locator(".chat-welcome")).to_be_visible()
        page.get_by_text("Расходы и время сессии", exact=True).click()
        expect(page.locator(".session-accounting dd").first).to_have_text("0")
        page.get_by_text("Расходы и время сессии", exact=True).click()

        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator(".chat-item").last.click()
        expect(composer).to_have_value(draft)
        first_chat = initial_chat.value.json()
        input_data = json.loads(
            (ROOT / "backend/tests/fixtures/network-f02.json").read_bytes()
        )
        saved = page.request.post(
            f"{chats_url}/{first_id}/designs",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            data={"branch_id": first_chat["default_branch_id"], "input": input_data},
        )
        assert saved.status == 201
        page.get_by_role("button", name="Проект Синоникса", exact=True).click()
        project = page.get_by_role("dialog")
        expect(project.locator(".diagram-viewer svg")).to_be_visible(timeout=20000)
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            no_horizontal_overflow()
            rules = project.get_by_role("region", name="Рассчитанные правила 1.9")
            expect(rules.locator("tbody tr")).to_have_count(3)
            with page.expect_download() as exported:
                project.get_by_role("button", name="Схема draw.io", exact=True).click()
            diagram = fromstring(exported.value.path().read_bytes())
            assert len(json.loads(diagram.attrib["data-rule-rows"])) == 3
        project.get_by_role("button", name="К чату", exact=True).click()
        expect(project).to_have_count(0)
        expect(composer).to_have_value(draft)

        def lose_admission_response(route):
            assert route.fetch().status == 202
            route.fulfill(status=503, json={})

        runs_path = f"/{first_id}/runs"
        page.route(chats_url + runs_path, lose_admission_response, times=1)
        with page.expect_response(post_to(runs_path)) as lost:
            send.click()
        expect(composer).to_have_value(draft)
        with page.expect_response(post_to(runs_path)) as retried:
            page.get_by_role("button", name="Повторить отправку", exact=True).click()
        assert retried.value.status == 202
        assert (
            retried.value.request.headers["idempotency-key"]
            == lost.value.request.headers["idempotency-key"]
        )
        expect(composer).to_have_value("")
        snapshot = page.request.get(f"{chats_url}/{first_id}/snapshot").json()
        assert len(snapshot["runs"]) == 1
        submitted = [message for message in snapshot["messages"] if message["role"] == "user"]
        assert [message["content"] for message in submitted] == [draft]

        run = snapshot["runs"][0]
        run["status"] = "succeeded"
        run["outcome"] = {"citations": [{
            "evidence_id": str(uuid4()),
            "title": "Учебный справочник",
            "location": "Раздел 3",
            "quote": "Пример цитаты для проверки интерфейса.",
        }]}
        snapshot["messages"] = [*submitted, {
            **submitted[0],
            "id": str(uuid4()),
            "role": "assistant",
            "run_id": run["id"],
            "ordinal": submitted[0]["ordinal"] + 1,
            "content": "Учебный ответ со ссылкой на справочник.",
        }]
        snapshot_path = re.compile(rf"/api/v1/chats/{first_id}/snapshot(?:\?.*)?$")
        page.route(snapshot_path, lambda route: route.fulfill(json=snapshot))
        page.get_by_role("button", name="Показать список чатов").click()
        page.locator(".chat-item").last.click()
        source = page.get_by_label("Источники ответа").get_by_text("Учебный справочник")
        source.click()
        expect(page.get_by_text("Раздел 3", exact=True)).to_be_visible()
        expect(page.get_by_text("Пример цитаты для проверки интерфейса.")).to_be_visible()
        no_horizontal_overflow()
        page.unroute(snapshot_path)
    finally:
        for chat_id in created:
            assert page.request.delete(f"{chats_url}/{chat_id}", headers=headers).status == 204


with sync_playwright() as session:
    browser = session.chromium.launch(
        **(
            {"executable_path": os.environ["CHROMIUM_PATH"]}
            if os.environ.get("CHROMIUM_PATH")
            else {}
        )
    )
    page = browser.new_page(
        viewport={"width": 1440, "height": 1000}, reduced_motion="reduce"
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(ORIGIN)
    expect(
        page.get_by_role("heading", name="Опишите сети. Соберите решение.")
    ).to_be_visible()
    expect(page.get_by_label("Логин", exact=True)).to_be_visible()
    assert page.get_by_role("button", name="Регистрация", exact=True).count() == 0
    page.wait_for_function(
        "Array.from(document.querySelectorAll('img.partner-logo'))"
        ".every(i => i.complete && i.naturalWidth > 0)"
    )

    def bounds():
        page.evaluate(
            "() => new Promise(resolve => "
            "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        ), "Horizontal overflow"
        for control in [
            page.get_by_label("Логин", exact=True),
            page.get_by_label("Пароль", exact=True),
        ]:
            box = control.bounding_box()
            assert (
                box
                and box["x"] >= 0
                and box["x"] + box["width"] <= page.viewport_size["width"]
            )

    bounds()
    page.screenshot(path=str(PRIVATE / "entry-desktop.png"), full_page=True)
    for theme in ["dark", "glass", "mixed", "standard"]:
        page.get_by_role("button", name="Настройки оформления").click()
        page.locator(f'[data-theme-choice="{theme}"]').click()
        page.keyboard.press("Escape")
        expect(page.get_by_label("Логин", exact=True)).to_be_visible()
        bounds()
        expect(page.get_by_role("img", name="АйТи Бастион", exact=True)).to_be_visible()
        if theme == "dark":
            page.screenshot(path=str(PRIVATE / "entry-dark.png"), full_page=True)
    for width in [390, 320]:
        page.set_viewport_size({"width": width, "height": 844})
        bounds()
        page.screenshot(path=str(PRIVATE / f"entry-mobile-{width}.png"), full_page=True)
        page.get_by_role("link", name="Перейти ко входу").click()
        expect(page.get_by_role("button", name="Войти", exact=True)).to_be_in_viewport()
        page.screenshot(path=str(PRIVATE / f"entry-login-{width}.png"), full_page=True)
        page.locator(".auth-conversation").evaluate("element => element.scrollTop = 0")
    # Test the real authentication behavior after taking anonymous-only screenshots.
    page.get_by_label("Логин", exact=True).fill(account["login"])
    page.get_by_label("Пароль", exact=True).fill("deliberately-wrong-password")
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("alert")).to_be_visible()
    expect(page.get_by_label("Пароль", exact=True)).to_have_value("")
    expect(page.get_by_label("Логин", exact=True)).to_have_value(account["login"])
    page.get_by_label("Пароль", exact=True).fill(account["password"])
    with page.expect_response("**/api/v1/auth/login") as login:
        page.get_by_role("button", name="Войти", exact=True).click()
    assert login.value.status == 200
    expect(page.locator("button.new-chat")).to_be_enabled(timeout=20000)
    check_workspace(page, login.value.json()["csrf_token"])
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.get_by_role("button", name="Выйти", exact=True).click()
    expect(page.get_by_label("Логин", exact=True)).to_be_visible()
    bounds()
    assert not errors, "Unexpected page error"
    browser.close()
print(
    "Entry, login, workspace retry, isolated drafts, accounting and draw.io passed."
)
