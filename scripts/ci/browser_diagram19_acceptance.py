"""F02/F07 through authenticated saved revisions, SVG and downloaded native XML."""

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import fromstring

from playwright.sync_api import expect, sync_playwright

assert os.environ.get("PARTNER_DIAGRAM_FIXTURE_CONFIRMED") == "1", "Use an isolated fixture"
ROOT = Path(__file__).resolve().parents[2]
REPORT = Path(os.environ.get("PARTNER_DIAGRAM_REPORT_DIR", str(ROOT / ".local/diagram19")))
REPORT.mkdir(parents=True, exist_ok=True)
account = json.loads(Path(os.environ["PARTNER_DIAGRAM_BROWSER_ACCOUNT_FILE"]).read_text(
    encoding="utf-8-sig",
))
ORIGIN = "http://localhost:8080"

with sync_playwright() as driver:
    browser = driver.chromium.launch(**({"executable_path": os.environ["CHROMIUM_PATH"]}
                                        if os.environ.get("CHROMIUM_PATH") else {}))
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    created = []
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    page.goto(ORIGIN)
    page.get_by_label("Логин", exact=True).fill(account["login"])
    page.get_by_label("Пароль", exact=True).fill(account["password"])
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.locator(".new-chat")).to_be_enabled(timeout=20000)

    def headers():
        token = page.request.get(ORIGIN + "/api/v1/auth/csrf").json()["csrf_token"]
        return {"Origin": ORIGIN, "X-CSRF-Token": token, "Idempotency-Key": str(uuid4())}

    try:
        for case in ("f02", "f07"):
            page.set_viewport_size({"width": 1440, "height": 1000})
            chat = page.request.post(ORIGIN + "/api/v1/chats", headers=headers(),
                                     data={"title": "Synthetic diagram " + case}).json()
            created.append(chat["id"])
            base = ORIGIN + f"/api/v1/chats/{chat['id']}"
            raw = json.loads((ROOT / f"backend/tests/fixtures/network-{case}.json").read_text(
                encoding="utf-8",
            ))

            def save(conditions, **extra):
                response = page.request.post(base + "/designs", headers=headers(), data={
                    "branch_id": chat["default_branch_id"], "input": conditions, **extra,
                })
                assert response.status == 201, response.status
                return response.json()

            first = save(raw)
            assert first["result"]["confirmed_rules_count"] == 3
            old_scene = page.request.get(base + f"/designs/{first['id']}/diagram").body()
            old_xml = page.request.post(base + f"/designs/{first['id']}/download/drawio",
                                        headers=headers()).body()
            page.reload()
            page.get_by_role("button", name="Проект Синоникса", exact=True).click()
            panel = page.get_by_role("dialog")
            svg = panel.locator(".diagram-viewer svg")
            expect(svg).to_be_visible(timeout=20000)
            assert svg.locator("[marker-start]").count() == 2
            if case == "f07":
                usb = svg.locator("[data-edge-id$='-usb']")
                assert usb.get_attribute("marker-end") is not None
            original_view = svg.get_attribute("viewBox")
            panel.get_by_role("button", name="Увеличить схему", exact=True).click()
            assert svg.get_attribute("viewBox") != original_view
            svg.press("ArrowRight")
            panel.get_by_role("button", name="Вся схема", exact=True).click()
            assert svg.get_attribute("viewBox") == original_view
            with page.expect_download() as exported:
                panel.get_by_role("button", name="Схема draw.io", exact=True).click()
            xml = exported.value.path().read_bytes()
            assert xml == old_xml
            (REPORT / f"{case}.drawio").write_bytes(xml)
            assert len(json.loads(fromstring(xml).attrib["data-rule-rows"])) == 3
            svg.screenshot(path=str(REPORT / f"{case}.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            expect(svg).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            revised = deepcopy(raw)
            next(system for system in revised["systems"] if system["id"] == "clock")[
                "name"
            ] = "Новое имя часов"
            save(revised, parent_id=first["id"])
            page.reload()
            page.get_by_role("button", name="Проект Синоникса", exact=True).click()
            expect(svg.locator("tspan").filter(
                has_text=re.compile("^Новое имя часов$"),
            )).to_be_visible(timeout=20000)
            assert page.request.get(base + f"/designs/{first['id']}/diagram").body() == old_scene
            assert page.request.post(base + f"/designs/{first['id']}/download/drawio",
                                     headers=headers()).body() == old_xml
            assert page.request.get(base + "/accounting").json()["summary"]["calls_count"] == 0
        assert not errors, errors
    finally:
        for chat_id in created:
            assert page.request.delete(ORIGIN + f"/api/v1/chats/{chat_id}",
                                        headers=headers()).status == 204
        browser.close()
print("F02/F07: SVG, USB arrow, zoom/fit/pan, mobile, native download and immutable history passed")
