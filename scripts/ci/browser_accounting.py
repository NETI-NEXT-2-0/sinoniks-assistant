"""Real browser + API accounting smoke; isolated offline-fixture environment only."""

import json
import os
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".local/ci-private"
account_path = Path(os.environ.get(
    "PARTNER_ACCOUNTING_BROWSER_ACCOUNT_FILE", str(PRIVATE / "probe.json"),
))
account_data = json.loads(account_path.read_text(encoding="utf-8-sig"))
account = account_data.get("account", account_data)
origin = "http://localhost:8080"


def verify_formatting_contract(page, ledger, path):
    """Synthetic UI-only values, separate from the actual API/download proof."""
    priced = json.loads(json.dumps(ledger))
    priced["summary"].update(estimated_cost_usd="1.08E-7", input_cost_usd="1.8E-8",
                             output_cost_usd="9E-8", provider_cost_usd="0.0018")
    page.route(path, lambda route: route.fulfill(json=priced))
    panel = page.locator(".session-accounting")
    panel.locator("summary").click()
    expect(panel).to_contain_text("$1.0800e-7")
    expect(panel).to_contain_text("$0.0018")
    priced["summary"].update(estimated_cost_usd=None)
    priced["accounting_complete"] = False
    panel.get_by_role("button", name="Обновить", exact=True).click()
    expect(panel).to_contain_text("Учёт неполный")
    expect(panel).to_contain_text("неизвестно")
    page.unroute(path)


with sync_playwright() as session:
    browser = session.chromium.launch(**({"executable_path": os.environ["CHROMIUM_PATH"]}
                                        if os.environ.get("CHROMIUM_PATH") else {}))
    page = browser.new_page(viewport={"width": 1365, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    page.goto(origin)
    page.get_by_label("Логин", exact=True).fill(account["login"])
    page.get_by_label("Пароль", exact=True).fill(account["password"])
    page.get_by_role("button", name="Войти", exact=True).click()
    new_chat = page.locator("button.new-chat")
    expect(new_chat).to_be_enabled(timeout=20000)
    new_chat.click()
    expect(new_chat).to_be_enabled()
    question = "Accounting browser observation " * 12
    page.get_by_role("textbox", name="Сообщение", exact=True).fill(question)
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url.endswith("/runs")) as admission:
        page.get_by_role("button", name="Отправить сообщение", exact=True).click()
    accepted = admission.value.json()
    run_id = accepted["run_id"]
    expect(page.locator(".assistant-message .prose").last).to_contain_text(
        question[:50], timeout=60000,
    )
    chat_id = admission.value.url.split("/chats/", 1)[1].split("/", 1)[0]
    path = origin + f"/api/v1/chats/{chat_id}/accounting"
    deadline = time.monotonic() + 30
    while True:
        ledger = page.request.get(path).json()
        if ledger["timing_complete"]:
            break
        assert time.monotonic() < deadline, "Client did not record render completion"
        page.wait_for_timeout(500)
    assert ledger["summary"]["calls_count"] == 0, "Requires offline fixture, no paid requests"
    assert ledger["mean_elapsed_ms"] > 0
    assert ledger["turns"][0]["turn_id"] == run_id
    assert ledger["accounting_complete"] is True
    panel = page.locator(".session-accounting")
    panel.locator("summary").click()
    expect(panel).to_contain_text("Всего по снимку тарифов")
    expect(panel).to_contain_text("$0")
    expect(panel).not_to_contain_text("замер неполный")
    with page.expect_download() as downloaded:
        panel.get_by_role("button", name="Скачать журнал JSON").click()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    output = PRIVATE / "accounting-browser.json"
    downloaded.value.save_as(output)
    assert json.loads(output.read_text())["turns"][0]["elapsed_ms"] == ledger["mean_elapsed_ms"]
    page.reload()
    expect(page.locator("button.new-chat")).to_be_enabled()
    restored = page.request.get(path).json()
    assert restored["turns"] == ledger["turns"], "Reload changed the original observation"
    verify_formatting_contract(page, ledger, path)
    assert not errors, "Browser runtime error"
    browser.close()
print("Accounting UI, full answer timing, JSON download and reload verified")
