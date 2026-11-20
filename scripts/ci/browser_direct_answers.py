"""Direct chat choices are real timed turns but never provider attempts."""

import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

if os.environ.get("PARTNER_DIAGRAM_FIXTURE_CONFIRMED") != "1":
    raise SystemExit("Requires the isolated loopback fixture, never a paid endpoint")
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
    with page.expect_response(
        lambda response: (
            response.request.method == "POST" and response.url.endswith("/api/v1/chats")
        )
    ) as creation:
        page.locator("button.new-chat").click()
    assert creation.value.status == 201
    chat = creation.value.json()
    base = ORIGIN + f"/api/v1/chats/{chat['id']}/designs"
    ledger_url = ORIGIN + f"/api/v1/chats/{chat['id']}/accounting"
    requested = []

    def question_response(route):
        requested.append(route.request.url)
        response = route.fetch()
        if len(requested) == 1:
            time.sleep(2)
            ledger = page.request.get(ledger_url).json()
            assert ledger["mean_full_elapsed_ms"] is None, "Timing ended before choices loaded"
        route.fulfill(response=response)

    page.route("**/api/v1/chats/*/designs/*/questions?*", question_response)

    def send_missing():
        page.get_by_role("textbox", name="Сообщение", exact=True).fill(
            "Синтетический неизвестный протокол: уточни обмен журналами."
        )
        page.get_by_role("button", name="Отправить сообщение", exact=True).click()

    send_missing()
    card = page.get_by_role("region", name="Проект Синоникса · версия 1", exact=True)
    choices = card.get_by_role("region", name="Уточнения проекта", exact=True)
    select = choices.get_by_role("combobox", name=re.compile("^Выберите вариант"))
    expect(select).to_be_visible(timeout=60000)
    expect(page.get_by_text("Готово", exact=True)).to_have_count(1, timeout=60000)
    first = page.request.get(base + "/latest?branch_id=" + chat["default_branch_id"]).json()[
        "design"
    ]
    assert first["input"]["flows"][0]["protocol"] is None
    choices.screenshot(path=str(PRIVATE / "direct-options-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    expect(select).to_be_visible()
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    bounds = choices.bounding_box()
    assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
    choices.screenshot(path=str(PRIVATE / "direct-options-mobile.png"))
    assert len(requested) == 1
    page.set_viewport_size({"width": 1365, "height": 1000})
    select.select_option(label="UDP")
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/runs")
    ) as direct:
        choices.get_by_role("button", name="Сохранить ответ", exact=True).click()
    assert direct.value.status == 202
    command = direct.value.request.post_data_json["project_answer"]
    assert command["base_revision_id"] == first["id"] and command["value"] == "UDP"
    second_card = page.get_by_role("region", name="Проект Синоникса · версия 2", exact=True)
    expect(second_card).to_be_visible(timeout=60000)
    expect(page.get_by_text("Готово", exact=True)).to_have_count(2, timeout=60000)
    ledger = page.request.get(ledger_url).json()
    assert ledger["summary"]["calls_count"] == 1 and len(ledger["turns"]) == 2
    outcome = page.request.get(ORIGIN + "/api/v1/runs/" + direct.value.json()["run_id"]).json()[
        "outcome"
    ]
    assert outcome["execution"] == "local_project_answer" and outcome["provider_calls"] == 0
    assert not page.request.get(
        base + f"/{first['id']}/questions?branch_id=" + chat["default_branch_id"]
    ).json()["current"]
    send_missing()
    third_card = page.get_by_role("region", name="Проект Синоникса · версия 3", exact=True)
    unknown = third_card.get_by_role("button", name="Не знаю", exact=True)
    expect(unknown).to_be_visible(timeout=60000)
    unknown.click()
    fourth = page.get_by_role("region", name="Проект Синоникса · версия 4", exact=True)
    expect(fourth).to_be_visible(timeout=60000)
    expect(page.get_by_text("Готово", exact=True)).to_have_count(4, timeout=60000)
    current = page.request.get(base + "/latest?branch_id=" + chat["default_branch_id"]).json()[
        "design"
    ]
    assert current["input"]["flows"][0]["protocol"] is None
    assert current["input"]["unknowns"][0]["reason"] == "user_unknown"
    ledger = page.request.get(ledger_url).json()
    assert ledger["summary"]["calls_count"] == 2 and len(ledger["turns"]) == 4
    deadline = time.monotonic() + 15
    while any(turn["full_elapsed_ms"] is None for turn in ledger["turns"]):
        assert time.monotonic() < deadline, "Missing full local-answer timing"
        page.wait_for_timeout(200)
        ledger = page.request.get(ledger_url).json()
    before = ledger["turns"]
    page.screenshot(path=str(PRIVATE / "direct-answers-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    assert len(requested) == 3, "Resize refetched question pages"
    assert page.request.get(ledger_url).json()["turns"] == before
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(PRIVATE / "direct-answers-mobile.png"))
    assert not errors, errors
    token = page.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
    assert (
        page.request.delete(
            ORIGIN + f"/api/v1/chats/{chat['id']}",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": token,
            },
        ).status
        == 204
    )
    browser.close()
print(
    "Four timed chat turns, two provider calls; typed UDP/unknown answers and stale scope verified"
)
