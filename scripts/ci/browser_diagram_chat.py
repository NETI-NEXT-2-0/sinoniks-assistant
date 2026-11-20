"""Full diagram timing against an explicitly configured loopback provider fixture.

No model-quality or real-provider latency claim. Not run against a paid endpoint.
"""

import json
import os
import time
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

if os.environ.get("PARTNER_DIAGRAM_FIXTURE_CONFIRMED") != "1":
    raise SystemExit("Requires the isolated diagram-provider-fixture environment")
account = json.loads(Path(os.environ["PARTNER_DIAGRAM_BROWSER_ACCOUNT_FILE"])
                     .read_text(encoding="utf-8-sig"))
ORIGIN = "http://localhost:8080"
PRIVATE = Path(__file__).resolve().parents[2] / ".local/ci-private"

with sync_playwright() as session:
    browser = session.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
    page = browser.new_page(viewport={"width": 1365, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    page.goto(ORIGIN)
    page.get_by_label("Логин", exact=True).fill(account["login"])
    page.get_by_label("Пароль", exact=True).fill(account["password"])
    page.get_by_role("button", name="Войти", exact=True).click()
    new_chat = page.locator("button.new-chat")
    expect(new_chat).to_be_enabled(timeout=20000)
    profile = page.request.get(ORIGIN + "/api/v1/auth/me").json()["runtime_profile"]
    assert profile == "openai_compatible"
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url.endswith("/api/v1/chats")) as creation:
        new_chat.click()
    assert creation.value.status == 201
    created_id = creation.value.json()["id"]
    # Explicit legacy project: this smoke exercises the archived v2 renderer,
    # while a separate fresh-chat smoke covers the default 1.9 extraction.
    seeded = page.request.post(ORIGIN + f"/api/v1/chats/{created_id}/designs", headers={
        "Origin": ORIGIN,
        "X-CSRF-Token": creation.value.request.headers["x-csrf-token"],
        "Idempotency-Key": str(uuid.uuid4()),
    }, data={"branch_id": creation.value.json()["default_branch_id"], "input": {}})
    assert seeded.status == 201
    expect(new_chat).to_be_enabled()
    held = []
    requested = []

    def diagram_response(route):
        requested.append(route.request.url)
        if len(held) >= 2:
            route.continue_()  # Later navigation/resize must not reopen a completed step.
            return
        path = route.request.url.split("/designs/", 1)[0] + "/accounting"
        if not held:
            held.append("failed-once")
            route.fulfill(status=503, json={"code": "test_fixture_unavailable"})
            return
        response = route.fetch()
        time.sleep(2.5)  # Deliberate resource delay, not a model-speed benchmark.
        ledger = page.request.get(path).json()
        assert ledger["mean_full_elapsed_ms"] is None, "Timing ended while the diagram was held"
        held.append("verified-before-release")
        route.fulfill(response=response)

    page.route("**/api/v1/chats/*/designs/*/diagram", diagram_response)
    page.get_by_role("textbox", name="Сообщение", exact=True).fill(
        "Синтетический запрос для проверки полной отрисовки проекта",
    )
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url.endswith("/runs")) as admission:
        page.get_by_role("button", name="Отправить сообщение", exact=True).click()
    run_id = admission.value.json()["run_id"]
    chat_id = admission.value.url.split("/chats/", 1)[1].split("/", 1)[0]
    assert chat_id == created_id
    path = ORIGIN + f"/api/v1/chats/{chat_id}/accounting"
    retry = page.get_by_role("button", name="Повторить загрузку", exact=True)
    expect(retry).to_be_visible(timeout=60000)
    expect(page.get_by_text("Показываем результат…", exact=True)).to_be_visible()
    assert page.get_by_text("Готово", exact=True).count() == 0
    assert page.request.get(path).json()["mean_full_elapsed_ms"] is None
    retry.click()
    card = page.locator(".design-chat-card")
    expect(card.locator("svg")).to_be_visible()
    expect(card.get_by_role("region", name="Правила сохранённой схемы")).to_be_visible()
    assert card.locator("table tbody tr").count() == 1
    deadline = time.monotonic() + 30
    while True:
        ledger = page.request.get(path).json()
        if ledger["full_timing_complete"]:
            break
        assert time.monotonic() < deadline, "No full render observation"
        page.wait_for_timeout(500)
    assert held == ["failed-once", "verified-before-release"]
    assert ledger["mean_full_elapsed_ms"] >= 2500
    assert ledger["turns"][0]["turn_id"] == run_id
    assert ledger["turns"][0]["timing_scope"] == "full_result"
    assert ledger["summary"]["calls_count"] == 1
    assert ledger["calls"][0]["model_id"] == "synthetic-diagram"
    assert ledger["calls"][0]["usage"]["input_tokens"] == 100
    expect(page.get_by_text("Готово", exact=True)).to_be_visible()
    before = ledger["turns"]
    card.get_by_role("button", name="Увеличить схему").click()
    assert page.request.get(path).json()["turns"] == before
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    assert len(requested) == 2, "Resize refetched a saved diagram"
    PRIVATE.mkdir(parents=True, exist_ok=True)
    card.screenshot(path=str(PRIVATE / "diagram-chat.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    expect(card.locator("svg")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.request.get(path).json()["turns"] == before
    assert not errors, "Browser runtime error"
    (PRIVATE / "diagram-chat-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    token = page.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
    removed = page.request.delete(ORIGIN + f"/api/v1/chats/{created_id}", headers={
        "Origin": ORIGIN, "X-CSRF-Token": token,
    })
    assert removed.status == 204
    browser.close()
print("Full SVG/table/text timing waits through failure, retry and resource delay; "
      "one fixture call")
