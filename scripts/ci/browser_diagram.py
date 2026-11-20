"""Local SVG scene and editable export on actual authenticated saved revisions."""

import json
import os
import uuid
from pathlib import Path
from xml.etree.ElementTree import fromstring

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".local/ci-private"
account_path = Path(os.environ.get("PARTNER_DIAGRAM_BROWSER_ACCOUNT_FILE",
                                   str(PRIVATE / "probe.json")))
data = json.loads(account_path.read_text(encoding="utf-8-sig"))
account = data.get("account", data)
ORIGIN = "http://localhost:8080"

with sync_playwright() as session:
    browser = session.chromium.launch(**({"executable_path": os.environ["CHROMIUM_PATH"]}
                                        if os.environ.get("CHROMIUM_PATH") else {}))
    page = browser.new_page(viewport={"width": 1365, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    page.goto(ORIGIN)
    page.get_by_label("Логин", exact=True).fill(account["login"])
    page.get_by_label("Пароль", exact=True).fill(account["password"])
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.locator("button.new-chat")).to_be_enabled(timeout=20000)
    csrf = page.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    response = page.request.post(ORIGIN + "/api/v1/chats", headers=headers,
                                  data={"title": "Diagram browser synthetic"})
    assert response.status == 201
    chat = response.json()
    flow = {"sender_side": "A", "sender": "App1", "receiver": "Приёмник <&>",
            "mode": "diode", "service": "Журналы", "policy": "Только журналы",
            "protocol": "UDP", "destination_port": 514}
    conditions = {"network_a": "Офис", "network_b": "Производство", "firewall": "A",
                  "exchanges": [flow | {"sender": sender} for sender in ("App1", "App2", "App3")]}
    base = ORIGIN + f"/api/v1/chats/{chat['id']}/designs"

    def save(body):
        token = page.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
        current = headers | {"Idempotency-Key": str(uuid.uuid4()), "X-CSRF-Token": token}
        result = page.request.post(base, headers=current,
                                   data=body)
        assert result.status == 201, f"Save failed: HTTP {result.status}"
        return result.json()

    first = save({"branch_id": chat["default_branch_id"], "input": conditions})
    old_scene = page.request.get(base + f"/{first['id']}/diagram").json()
    assert first["result"]["logical_rule_count"] == 1
    page.reload()
    page.get_by_role("button", name="Проект Синоникса", exact=True).click()
    panel = page.get_by_role("dialog", name="Проект Синоникса")
    svg = panel.locator(".diagram-viewer svg")
    expect(svg).to_be_visible()
    assert svg.locator("[data-node-id^='system-A-']").count() == 3
    assert svg.locator("[data-node-id='sinoniks']").count() == 1
    original_view = svg.get_attribute("viewBox")
    panel.get_by_role("button", name="Увеличить схему").click()
    assert svg.get_attribute("viewBox") != original_view
    panel.get_by_role("button", name="Вся схема", exact=True).click()
    assert svg.get_attribute("viewBox") == original_view
    full = panel.get_by_role("button", name="На весь экран", exact=True)
    if full.count():
        full.click()
        expect(panel.get_by_role("button", name="Свернуть", exact=True)).to_be_visible()
        assert page.evaluate("document.fullscreenElement !== null")
        panel.get_by_role("button", name="Свернуть", exact=True).click()
        expect(panel).to_be_visible()
    PRIVATE.mkdir(parents=True, exist_ok=True)
    panel.screenshot(path=str(PRIVATE / "diagram-desktop.png"))
    for edge in old_scene["scene"]["pages"][0]["edges"]:
        expected = " ".join(",".join(map(str, point)) for point in edge["points"])
        assert svg.locator(f"[data-edge-id='{edge['id']}']").get_attribute("points") == expected
    with page.expect_download() as downloaded:
        panel.get_by_role("button", name="Скачать схему draw.io", exact=True).click()
    target = PRIVATE / "diagram-browser.drawio"
    downloaded.value.save_as(target)
    root = fromstring(target.read_text())
    assert len(root.findall("diagram")) == 1
    assert root.find(".//mxCell[@id='system-A-3']") is not None
    conditions["exchanges"] += [flow | {"receiver": f"Система {n}"} for n in range(3)]
    second = save({"branch_id": chat["default_branch_id"], "parent_id": first["id"],
                   "input": conditions})
    assert second["result"]["logical_rule_count"] == 4
    assert page.request.get(base + f"/{first['id']}/diagram").json() == old_scene
    page.reload()
    page.get_by_role("button", name="Проект Синоникса", exact=True).click()
    expect(svg).to_be_visible()
    select = panel.locator(".diagram-toolbar select")
    assert select.locator("option").count() == 2
    select.select_option("1")
    expect(svg).to_have_attribute("aria-label", "Схема Синоникса · Правила 4–4")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(svg).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    panel.locator(".diagram-viewer").screenshot(path=str(PRIVATE / "diagram-mobile.png"))
    assert not errors, "Browser runtime error"
    token = page.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
    removed = page.request.delete(ORIGIN + f"/api/v1/chats/{chat['id']}", headers={
        "Origin": ORIGIN, "X-CSRF-Token": token,
    })
    assert removed.status == 204
    browser.close()
print("Saved SVG geometry, zoom/fit, pages, editable download, mobile and old revision verified")
