"""Product entry surface and unchanged real login on an explicitly local fixture."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

assert os.environ.get("PARTNER_ENTRY_FIXTURE_CONFIRMED") == "1"
ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".local/ci-private"
PRIVATE.mkdir(parents=True, exist_ok=True)
account = json.loads(
    Path(os.environ["PARTNER_ENTRY_BROWSER_ACCOUNT_FILE"]).read_bytes()
)
ORIGIN = "http://localhost:8080"

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
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.locator("button.new-chat")).to_be_enabled(timeout=20000)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.get_by_role("button", name="Выйти", exact=True).click()
    expect(page.get_by_label("Логин", exact=True)).to_be_visible()
    bounds()
    assert not errors, "Unexpected page error"
    browser.close()
print(
    "Entry desktop/mobile/themes, asset decode, real invalid login/login/logout passed."
)
