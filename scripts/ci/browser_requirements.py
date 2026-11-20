"""Authenticated browser acceptance for explicit upgrade and v3 unknown/history editing."""

import json
import os
import re
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".local/ci-private"
PRIVATE.mkdir(parents=True, exist_ok=True)
data = json.loads(
    Path(
        os.environ.get("PARTNER_DIAGRAM_BROWSER_ACCOUNT_FILE", str(PRIVATE / "probe.json"))
    ).read_text(encoding="utf-8-sig")
)
account = data.get("account", data)
ORIGIN = "http://localhost:8080"
with sync_playwright() as session:
    browser = session.chromium.launch(
        executable_path=os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium")
    )
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
        ORIGIN + "/api/v1/chats",
        headers=headers(),
        data={"title": "Requirements browser synthetic"},
    )
    assert response.status == 201
    chat = response.json()
    base = ORIGIN + f"/api/v1/chats/{chat['id']}/designs"
    flow = {
        "sender_side": "A",
        "sender": "Журналы",
        "receiver": "SIEM",
        "mode": "diode",
        "service": "Журналы",
        "policy": "Только журналы",
        "protocol": "UDP",
        "destination_port": 514,
    }
    saved = page.request.post(
        base,
        headers={**headers(), "Idempotency-Key": str(uuid.uuid4())},
        data={
            "branch_id": chat["default_branch_id"],
            "input": {
                "network_a": "Цех",
                "network_b": "Офис",
                "firewall": "B",
                "exchanges": [flow],
            },
        },
    )
    assert saved.status == 201
    original = saved.json()
    page.reload()
    page.get_by_role("button", name="Проект Синоникса", exact=True).click()
    old = page.get_by_role("dialog")
    expect(old.get_by_text("Сохранённая версия 1", exact=True)).to_be_visible()
    old.get_by_role("button", name="Перейти к требованиям 1.9", exact=True).click()
    panel = page.get_by_role("dialog")
    expect(
        panel.get_by_role("heading", name="Требования к Синониксу 1.9", exact=True)
    ).to_be_visible()
    expect(panel.get_by_text("Сохранена версия 2.", exact=False)).to_be_visible()
    trusts = panel.get_by_role("combobox", name="Доверие сети", exact=False)
    expect(trusts.nth(0)).to_have_value("")
    expect(trusts.nth(1)).to_have_value("")
    trusts.nth(0).select_option("closed")
    trusts.nth(1).select_option("open")
    panel.get_by_role("button", name="Сохранить требования", exact=True).click()
    expect(panel.get_by_text("Сохранена версия 3.", exact=False)).to_be_visible()
    expect(panel.get_by_role("heading", name="Основные сведения собраны")).to_be_visible()
    expect(panel.get_by_role("region", name="Рассчитанные правила 1.9")).to_be_visible()
    with page.expect_download() as expert_download:
        panel.get_by_role("button", name="Экспертный JSON", exact=True).click()
    expert_path = PRIVATE / "result-browser.json"
    expert_download.value.save_as(expert_path)
    expert = json.loads(expert_path.read_text())
    assert expert["schema_version"] == "1.1" and expert["confirmed_rules_count"] == 1
    first_v3 = page.request.get(base + "/latest?branch_id=" + chat["default_branch_id"]).json()[
        "design"
    ]
    ids = [item["id"] for item in first_v3["input"]["systems"]]
    panel.get_by_role("combobox", name=re.compile("^Протокол")).select_option("")
    panel.get_by_role("button", name="Сохранить требования", exact=True).click()
    expect(panel.get_by_text("Сохранена версия 4.", exact=False)).to_be_visible()
    panel.get_by_role("button", name="Не знаю", exact=True).click()
    panel.get_by_role("button", name="Сохранить требования", exact=True).click()
    expect(panel.get_by_text("Сохранена версия 5.", exact=False)).to_be_visible()
    expect(panel.get_by_text("Ответ «не знаю» сохранён.", exact=True)).to_be_visible()
    panel.get_by_label("Название системы", exact=True).nth(0).fill("Новый сборщик журналов")
    panel.get_by_role("button", name="Сохранить требования", exact=True).click()
    expect(panel.get_by_text("Сохранена версия 6.", exact=False)).to_be_visible()
    latest = page.request.get(base + "/latest?branch_id=" + chat["default_branch_id"]).json()[
        "design"
    ]
    assert [item["id"] for item in latest["input"]["systems"]] == ids
    assert latest["input"]["flows"][0]["protocol"] is None
    assert latest["input"]["unknowns"][0]["reason"] == "user_unknown"
    assert latest["status"] == "preliminary" and latest["result"]["confirmed_rules_count"] is None
    with page.expect_download() as download:
        panel.get_by_role("button", name="Скачать требования JSON", exact=True).click()
    path = PRIVATE / "requirements-browser.json"
    download.value.save_as(path)
    assert json.loads(path.read_text())["id"] == latest["input"]["id"]
    # Return to v2 archive through the revision chain, retaining the original SVG.
    for revision in (5, 4, 3, 2):
        panel.get_by_role("button", name="Предыдущая версия", exact=True).click()
        expect(panel.get_by_text(f"Сохранена версия {revision}.", exact=False)).to_be_visible()
    panel.get_by_role("button", name="Предыдущая версия", exact=True).click()
    expect(page.get_by_text("Сохранённая версия 1", exact=True)).to_be_visible()
    expect(page.get_by_role("img", name="Схема Синоникса · Правила 1–1")).to_be_visible()
    panel.get_by_role("button", name="К чату", exact=True).click()
    page.get_by_role("button", name="Проект Синоникса", exact=True).click()
    panel = page.get_by_role("dialog")
    expect(panel.get_by_label("Название системы", exact=True).nth(0)).to_have_value(
        "Новый сборщик журналов"
    )
    expect(panel.get_by_text("Ответ «не знаю» сохранён.", exact=True)).to_be_visible()
    panel.screenshot(path=str(PRIVATE / "requirements-desktop.png"))
    panel.get_by_label("Название системы", exact=True).nth(0).fill("Не потерять при resize")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(panel.get_by_label("Название системы", exact=True).nth(0)).to_have_value(
        "Не потерять при resize"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect(panel.get_by_role("button", name="К чату", exact=True)).to_be_visible()
    panel.screenshot(path=str(PRIVATE / "requirements-mobile.png"))
    old = page.request.get(base + "/" + original["id"]).json()
    assert old["artifact_sha256"] == original["artifact_sha256"]
    assert not errors, errors
    removed = page.request.delete(ORIGIN + f"/api/v1/chats/{chat['id']}", headers=headers())
    assert removed.status == 204
    browser.close()
print("V3 upgrade, history, unknowns, stable IDs, JSON, archived SVG and mobile verified")
