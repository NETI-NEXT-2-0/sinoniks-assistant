import argparse
import json
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from xml.etree.ElementTree import Element, fromstring

from playwright.sync_api import Page, Route, expect, sync_playwright

from partner_agent.sinoniks_calculation import solve_project
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_export import scene_drawio
from partner_agent.sinoniks_layout19 import layout19

INITIAL_NAME = "Сервер времени"
EDITED_NAME = "Сервер & <архив>"
HARNESS_PATH = "/_roundtrip.html"
HARNESS = b"""<!doctype html>
<style>body{margin:0}iframe{display:block;width:100vw;height:100vh;border:0}</style>
<script>
const messages = [];
addEventListener("message", ({origin, source, data}) => {
    if (origin === location.origin && source === document.querySelector("iframe").contentWindow) {
        messages.push(JSON.parse(data));
    }
});
</script>
<iframe id="editor" src="/index.html?embed=1&amp;proto=json&amp;lang=en&amp;saveAndExit=0"></iframe>
"""


class EditorHandler(SimpleHTTPRequestHandler):
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if code not in (200, 304, 404):
            super().log_request(code, size)

    def do_GET(self) -> None:
        if self.path != HARNESS_PATH:
            super().do_GET()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HARNESS)))
        self.end_headers()
        self.wfile.write(HARNESS)


def synthetic_export() -> str:
    request = Requirements19.model_validate({
        "id": "editor-smoke",
        "networks": [
            {"id": "office", "name": "Офис", "trust": "open"},
            {"id": "plant", "name": "Производство", "trust": "closed"},
        ],
        "systems": [
            {"id": "clock", "network_id": "office", "name": INITIAL_NAME},
            {"id": "clock-client", "network_id": "plant"},
            {"id": "telemetry", "network_id": "plant"},
            {"id": "collector", "network_id": "office"},
            {"id": "lab-usb", "name": "Тестовый USB", "kind": "usb", "attachment": "node-A"},
            {"id": "storage", "network_id": "plant", "kind": "storage"},
        ],
        "flows": [
            {
                "id": "clock-flow", "kind": "data", "sender_network_id": "office",
                "receiver_network_id": "plant", "sender_ids": ["clock"],
                "receiver_ids": ["clock-client"], "protocol": "UDP",
                "destination_port": 123, "mode": "one-way", "application": "ntp",
            },
            {
                "id": "telemetry-flow", "kind": "data", "sender_network_id": "plant",
                "receiver_network_id": "office", "sender_ids": ["telemetry"],
                "receiver_ids": ["collector"], "protocol": "TCP",
                "destination_port": 1883, "mode": "one-way",
            },
            {
                "id": "file-flow", "kind": "file", "direction": "forward",
                "sender_network_id": "office", "receiver_network_id": "plant",
                "sender_ids": ["lab-usb"], "receiver_ids": ["storage"],
                "sides": [
                    {"network_id": "office", "manual": False, "usb": True, "checks": []},
                    {
                        "network_id": "plant", "manual": False, "usb": False, "checks": [],
                        "automations": [
                            {"operation": "PUSH", "protocol": "SMB", "storage_id": "storage"},
                        ],
                    },
                ],
            },
        ],
    })
    return scene_drawio(layout19(request, solve_project(request)))


def open_editor(page: Page, origin: str, xml: str) -> None:
    page.goto(origin + HARNESS_PATH, wait_until="domcontentloaded")
    page.wait_for_function("messages.some(message => message.event === 'init')")
    page.evaluate(
        """payload => document.querySelector("iframe").contentWindow
            .postMessage(JSON.stringify(payload), location.origin)""",
        {"action": "load", "xml": xml, "title": "Round trip.drawio", "autosave": 0},
    )


def save_editor(page: Page) -> str:
    page.frame_locator("#editor").get_by_text("Save", exact=True).click()
    return page.wait_for_function(
        "messages.find(message => message.event === 'save')?.xml"
    ).json_value()


def rename_and_move(page: Page, original_name: str) -> None:
    frame = page.frame_locator("#editor")
    frame.get_by_text(original_name, exact=True).dblclick()
    frame.locator(".mxCellEditor[contenteditable=true]").fill(EDITED_NAME)
    page.keyboard.press("Control+Enter")
    renamed = frame.get_by_text(EDITED_NAME, exact=True)
    expect(renamed).to_be_visible()
    box = renamed.bounding_box()
    assert box is not None, "Edited system has no visible bounds"
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 40, y + 25, steps=10)
    page.mouse.up()


def position(cell: Element) -> tuple[float, float]:
    geometry = cell.find("mxGeometry")
    assert geometry is not None, "System has no draw.io geometry"
    return float(geometry.get("x", "0")), float(geometry.get("y", "0"))


def connections(root: Element) -> dict[str, tuple[str | None, str | None]]:
    return {
        edge.attrib["id"]: (edge.get("source"), edge.get("target"))
        for edge in root.findall(".//mxCell[@edge='1']")
    }


def edge_styles(root: Element) -> dict[str, dict[str, str | None]]:
    result = {}
    for edge in root.findall(".//mxCell[@edge='1']"):
        style = dict(part.split("=", 1) for part in edge.attrib["style"].split(";") if "=" in part)
        result[edge.attrib["id"]] = {key: style.get(key) for key in (
            "strokeColor", "startArrow", "endArrow",
        )}
    return result


def check_exports(original: str, saved: str, reopened: str, original_name: str) -> None:
    initial, edited, resaved = (fromstring(xml) for xml in (original, saved, reopened))
    start_cell = next((cell for cell in initial.findall(".//mxCell")
                       if cell.get("value") == original_name), None)
    assert start_cell is not None, "Editable system missing from export"
    cell_path = f".//mxCell[@id='{start_cell.attrib['id']}']"
    rules = json.loads(initial.attrib["data-rule-rows"])
    assert rules, "Expected at least one acceptance rule row"
    links = connections(initial)
    for edge in initial.findall(".//mxCell[@edge='1']"):
        if edge.attrib["id"].endswith("-usb"):
            assert {edge.get("source"), edge.get("target")} == {"usb-1", "node-A"}
    edit_position = None
    for root in (edited, resaved):
        cell = root.find(cell_path)
        assert cell is not None and cell.get("value") == EDITED_NAME, "Renamed system was lost"
        current = position(cell)
        assert current != position(start_cell), "Moved system kept its original position"
        assert edit_position is None or edit_position == current, "Position changed on reopen"
        edit_position = current
        assert json.loads(root.attrib["data-rule-rows"]) == rules, "Rule rows changed"
        assert connections(root) == links, "Edge endpoints changed"
        assert edge_styles(root) == edge_styles(initial), "Rule colors or directions changed"


def roundtrip(
    origin: str, channel: str | None, original: str, original_name: str,
    screenshots_dir: Path | None,
) -> None:
    def route_local(route: Route) -> None:
        if route.request.url.startswith(origin + "/"):
            route.continue_()
        else:
            route.abort()

    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=channel, **(
            {"executable_path": os.environ["CHROMIUM_PATH"]}
            if os.environ.get("CHROMIUM_PATH") else {}
        ))
        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      service_workers="block")
        context.route("**/*", route_local)
        page = context.new_page()
        open_editor(page, origin, original)
        if screenshots_dir:
            page.screenshot(path=str(screenshots_dir / "drawio-before-edit.png"))
        rename_and_move(page, original_name)
        saved = save_editor(page)

        reopened = context.new_page()
        open_editor(reopened, origin, saved)
        frame = reopened.frame_locator("#editor")
        expect(frame.get_by_text(EDITED_NAME, exact=True)).to_be_visible()
        if screenshots_dir:
            reopened.screenshot(path=str(screenshots_dir / "drawio-reopened.png"))
        check_exports(original, saved, save_editor(reopened), original_name)
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("editor_dir", type=Path)
    parser.add_argument("--channel", help="Installed Chromium channel, for example msedge")
    parser.add_argument("--input", type=Path, help="Downloaded acceptance export")
    parser.add_argument("--system-name", default=INITIAL_NAME, help="Exact editable system label")
    parser.add_argument("--screenshots-dir", type=Path, help="Directory for browser evidence")
    options = parser.parse_args()
    if not (options.editor_dir / "index.html").is_file():
        parser.error("editor_dir must point to the draw.io webapp directory")
    if options.screenshots_dir:
        options.screenshots_dir.mkdir(parents=True, exist_ok=True)

    handler = partial(EditorHandler, directory=str(options.editor_dir.resolve()))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            original = (options.input.read_text(encoding="utf-8") if options.input
                        else synthetic_export())
            roundtrip(f"http://127.0.0.1:{server.server_port}", options.channel, original,
                      options.system_name, options.screenshots_dir)
        finally:
            server.shutdown()
            worker.join()
    print("draw.io: renamed, moved, saved and reopened; rule rows and edge endpoints preserved")


if __name__ == "__main__":
    main()
