import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".local/ci-private"
ORIGIN = "http://localhost:8080"
KNOWLEDGE = "/api/v1/knowledge"
saved = PRIVATE / "browser.json"


def checked_json(response):
    assert response.ok, f"Unexpected HTTP status {response.status} for {response.request.method}"
    return response.json()


def api_session(client, credentials):
    csrf = client.get("/api/v1/auth/csrf").raise_for_status().json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login", json=credentials,
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    ).raise_for_status()
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def verify_web_search_controls(page):
    web = page.get_by_role("button", name="Веб-поиск", exact=True)
    web.click()
    unavailable = page.get_by_text("Администратор ещё не настроил поиск и список доменов.")
    expect(unavailable).to_be_visible()
    web.click()

    domain_path = "**/api/v1/web/official-domains"
    search_path = "**/api/v1/web/search"
    query = "Каталог курсов"
    url = "https://partner.example.org/catalog"
    sent = []

    def answer(route):
        sent.append(route.request.post_data_json)
        route.fulfill(json={
            "partial": False, "items": [{"url": url, "title": "Каталог", "excerpt": "Курсы"}],
        })

    page.route(domain_path, lambda route: route.fulfill(json={"domains": ["partner.example.org"]}))
    page.route(search_path, answer)
    try:
        web.click()
        page.get_by_label("Публичные ключевые слова").fill(query)
        search = page.get_by_role("button", name="Искать на сайте", exact=True)
        expect(search).to_be_disabled()
        page.get_by_role("checkbox", name="Подтверждаю, что запрос содержит").check()
        search.click()
        expect(page.get_by_role("link", name="Каталог", exact=True)).to_have_attribute("href", url)
        assert sent == [{"query": query, "domain": "partner.example.org", "confirmed_public": True}]
        web.click()
    finally:
        page.unroute(search_path)
        page.unroute(domain_path)


def verify_live_profile_presentation(page):
    def live_metadata(route):
        response = route.fetch()
        body = response.json()
        body["runtime_profile"] = "openai_compatible"
        route.fulfill(response=response, json=body)

    page.route("**/api/v1/auth/me", live_metadata)
    page.reload()
    expect(page.get_by_role("button", name="Проект Синоникса", exact=True)).to_be_visible()
    expect(page.get_by_text("Проверка офлайн-стенда", exact=True)).to_have_count(0)
    page.unroute("**/api/v1/auth/me", live_metadata)
    page.reload()
    expect(page.get_by_text("Проверка офлайн-стенда", exact=True)).to_be_visible()
    print("Live-profile presentation hides fixture controls (substitute session metadata)")


def verify_request_mode_controls(page):
    root = page.get_by_label("Ветка разговора").input_value()
    selector = page.get_by_label("Режим ответа", exact=True)
    expect(selector).to_have_value("auto")
    selector.select_option("reference")
    question = "Request mode acceptance"
    page.get_by_role("textbox", name="Сообщение", exact=True).fill(question)
    with page.expect_request(
        lambda request: request.method == "POST" and request.url.endswith("/runs")
    ) as sent:
        page.get_by_role("button", name="Отправить сообщение", exact=True).click()
    assert sent.value.post_data_json["request_mode"] == "reference"
    answer = page.locator(".assistant-message[data-message-id]").filter(has_text=question)
    expect(answer.locator(".prose")).to_contain_text(question, timeout=45000)
    expect(page.locator(".user-row").filter(has_text=question).get_by_text(
        "Справка", exact=True,
    )).to_be_visible()
    page.reload()
    expect(selector).to_have_value("reference")
    selector.select_option("design")
    answer.get_by_role("button", name="Повторить в новой ветке", exact=True).click()
    expect(page.get_by_label("Ветка разговора")).not_to_have_value(root)
    expect(selector).to_have_value("reference")
    expect(answer.locator(".prose")).to_contain_text(question, timeout=45000)
    page.get_by_label("Ветка разговора").select_option(root)
    expect(selector).to_have_value("design")
    selector.select_option("auto")
    print("Request mode admission, reload, inherited intent and branch draft verified")


def verify_diagram_export(panel, page, data):
    with page.expect_download() as download:
        panel.get_by_role("button", name="Скачать схему draw.io", exact=True).click()
    assert download.value.suggested_filename.endswith(".drawio")
    diagram = ElementTree.parse(download.value.path()).getroot()
    cells = diagram.findall("./diagram[@id='topology']/mxGraphModel/root/mxCell")
    edges = {cell.attrib["id"]: (cell.get("source"), cell.get("target"))
             for cell in cells if cell.get("edge") == "1"}
    assert edges == {
        item["id"]: (item["source"], item["target"]) for item in data["connections"]
    }
    vertices = {cell.attrib["id"] for cell in cells if cell.get("vertex") == "1"}
    assert {node["id"] for node in data["network_nodes"] + data["device_nodes"]} <= vertices
    expected_rules = {f"rule-{i}" for i in range(1, len(data["rules"]) + 1)}
    rule_labels = {cell.attrib["id"] for cell in diagram.findall(".//mxCell")} & expected_rules
    assert len(rule_labels) == data["counts"]["logical_exchanges"]


def verify_rehearsal_example(page):
    new_chat = page.locator("button.new-chat")
    new_chat.click()
    expect(new_chat).to_be_enabled()
    open_project = page.get_by_role("button", name="Проект Синоникса", exact=True)
    open_project.click()
    panel = page.get_by_role("dialog", name="Проект Синоникса", exact=True)
    example = panel.get_by_role("button", name="Загрузить учебный пример", exact=True)
    close = panel.get_by_role("button", name="К чату", exact=True)
    network = panel.get_by_label("Контур A", exact=True)
    reports = panel.get_by_role("group", name="Обмен 5", exact=True)
    receiver = reports.get_by_label("Получатель", exact=True)
    save = panel.get_by_role("button", name="Сохранить и рассчитать", exact=True)

    expect(example).to_be_visible()
    network.fill("Свой контур")
    expect(example).to_have_count(0)
    close.click()
    open_project.click()
    example.click()
    expect(network).to_have_value("Измерительный контур")
    expect(receiver).to_have_value("")
    expect(example).to_have_count(0)
    close.click()
    open_project.click()
    expect(example).to_be_visible()
    expect(network).to_have_value("")
    example.click()

    save.click()
    expect(panel.get_by_role("heading", name="Сохранённая версия 1", exact=True)).to_be_visible()
    expect(panel.get_by_text("Для продолжения уточните условия.", exact=True)).to_be_visible()
    expect(panel.locator(".design-result li")).to_have_count(1)
    expect(panel.locator(".design-result li")).to_have_text("Обмен 5 — кто получает?")
    expect(panel.get_by_role("button", name="Скачать JSON", exact=True)).to_have_count(0)
    expect(example).to_have_count(0)
    receiver.fill("Хранилище отчётов")
    save.click()
    expect(panel.get_by_role("heading", name="Сохранённая версия 2", exact=True)).to_be_visible()
    with page.expect_download() as download:
        panel.get_by_role("button", name="Скачать JSON", exact=True).click()
    data = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
    assert data["counts"]["input_requirements"] == 5
    assert data["counts"]["logical_exchanges"] == 4
    assert data["counts"]["network_slot_pairs"] == 3
    assert data["counts"]["network_node_settings"] == 6
    assert data["counts"]["file_transfer_policies"] == 1
    verify_diagram_export(panel, page, data)
    close.click()
    open_project.click()
    expect(receiver).to_have_value("Хранилище отчётов")
    expect(example).to_have_count(0)
    close.click()
    print("Rehearsal example, one clarification and exported counts verified")


def verify_saved_design(page):
    page.get_by_role("button", name="Проект Синоникса", exact=True).click()
    panel = page.get_by_role("dialog", name="Проект Синоникса", exact=True)
    panel.get_by_role("button", name="Сохранить и рассчитать", exact=True).click()
    expect(panel.get_by_text("Назовите контур A.", exact=True)).to_be_visible()
    panel.get_by_label("Контур A", exact=True).fill("Производство")
    panel.get_by_label("Контур B", exact=True).fill("Офис")
    panel.get_by_label("Требуемое размещение МЭ").select_option("B")
    panel.get_by_role("button", name="Добавить обмен", exact=True).click()
    panel.get_by_label("Откуда начинается обмен").select_option("A")
    panel.get_by_label("Отправитель", exact=True).fill("Датчик")
    panel.get_by_label("Получатель", exact=True).fill("Мониторинг")
    panel.get_by_label("Режим").select_option("diode")
    panel.get_by_label("Сервис или категория файлов").fill("Телеметрия")
    panel.get_by_label("Ограничения обмена").fill("Только мониторинг")

    def lose_reply(route):
        reply = route.fetch()
        assert reply.status == 201
        route.abort("failed")

    command_path = "**/api/v1/chats/*/designs"
    page.route(command_path, lose_reply, times=1)
    panel.get_by_role("button", name="Сохранить и рассчитать", exact=True).click()
    retry = panel.get_by_role("button", name="Повторить сохранение", exact=True)
    expect(retry).to_be_enabled()
    expect(panel.get_by_label("Контур A", exact=True)).to_be_disabled()
    retry.click()
    expect(panel.get_by_role("heading", name="Сохранённая версия 2", exact=True)).to_be_visible()
    with page.expect_download() as download:
        panel.get_by_role("button", name="Скачать JSON", exact=True).click()
    data = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
    assert data["networks"] == {"A": "Производство", "B": "Офис"}
    assert data["counts"]["logical_exchanges"] == 1 and data["counts"]["network_node_settings"] == 2
    panel.get_by_role("button", name="Предыдущая версия", exact=True).click()
    expect(panel.get_by_role("heading", name="Сохранённая версия 1", exact=True)).to_be_visible()
    expect(panel.get_by_label("Контур A", exact=True)).to_be_disabled()
    panel.get_by_role("button", name="К актуальной версии", exact=True).click()
    expect(panel.get_by_role("heading", name="Сохранённая версия 2", exact=True)).to_be_visible()
    panel.get_by_role("button", name="К чату", exact=True).click()
    page.reload()
    page.get_by_role("button", name="Проект Синоникса", exact=True).click()
    expect(panel.get_by_label("Контур A", exact=True)).to_have_value("Производство")
    expect(panel.get_by_role("heading", name="Сохранённая версия 2", exact=True)).to_be_visible()
    panel.get_by_role("button", name="К чату", exact=True).click()
    print("Saved design clarification, lost-reply retry, JSON download and reload verified")


def verify_design_branch_base(page):
    root = page.get_by_label("Ветка разговора").input_value()
    question = "Project branch acceptance"
    page.get_by_role("textbox", name="Сообщение", exact=True).fill(question)
    page.get_by_role("button", name="Отправить сообщение", exact=True).click()
    answer = page.locator(".assistant-message[data-message-id]").filter(has_text=question)
    expect(answer.locator(".prose")).to_contain_text(question, timeout=45000)
    source_message_id = answer.get_attribute("data-message-id")
    assert source_message_id
    answer.get_by_role("button", name="Повторить в новой ветке", exact=True).click()
    expect(page.get_by_label("Ветка разговора")).not_to_have_value(root)
    expect(answer.locator(".prose")).to_contain_text(question, timeout=45000)
    page.get_by_role("button", name="Проект Синоникса", exact=True).click()
    panel = page.get_by_role("dialog", name="Проект Синоникса", exact=True)
    expect(panel.get_by_label("Контур A", exact=True)).to_have_value("Производство")
    expect(panel.get_by_text(
        "Исходная версия унаследована из предыдущей ветки. Изменения сохранятся в текущей ветке.",
        exact=True,
    )).to_be_visible()
    panel.get_by_label("Контур B", exact=True).fill("Дочерний офис")
    panel.get_by_role("button", name="Сохранить и рассчитать", exact=True).click()
    expect(panel.get_by_role("heading", name="Сохранённая версия 3", exact=True)).to_be_visible()
    panel.get_by_role("button", name="К чату", exact=True).click()
    page.get_by_label("Ветка разговора").select_option(root)
    page.get_by_role("button", name="Проект Синоникса", exact=True).click()
    expect(panel.get_by_label("Контур B", exact=True)).to_have_value("Офис")
    expect(panel.get_by_role("heading", name="Сохранённая версия 2", exact=True)).to_be_visible()
    panel.get_by_role("button", name="К чату", exact=True).click()
    print("Project branch base, child edit and unchanged parent verified")
    return source_message_id


def verify_restored_project_context(page, source_message_id):
    root = page.get_by_label("Ветка разговора").input_value()
    target = page.locator(f'.assistant-message[data-message-id="{source_message_id}"]')
    target.get_by_role("button", name="Повторить в новой ветке", exact=True).click()
    expect(page.get_by_label("Ветка разговора")).not_to_have_value(root)
    page.get_by_role("button", name="Проект Синоникса", exact=True).click()
    panel = page.get_by_role("dialog", name="Проект Синоникса", exact=True)
    expect(panel.get_by_label("Контур A", exact=True)).to_have_value("Производство")
    expect(panel.get_by_label("Контур B", exact=True)).to_have_value("Офис")
    expect(panel.get_by_role("heading", name="Сохранённая версия 2", exact=True)).to_be_visible()
    panel.get_by_role("button", name="К чату", exact=True).click()
    page.get_by_label("Ветка разговора").select_option(root)
    print("Restored original run retains its manual project base when regenerated")


def verify_moderated_snapshot(page, probe):
    code = "BROW-" + uuid4().hex[:8]
    address = "https://partner.example.org/courses/browser"
    captured_on = "2026-11-20"
    removed = "review-only@example.test"
    original = f"Course {code}: send questions to {removed} during office hours."
    reviewed = original.replace(removed, "the help desk")
    page.goto(ORIGIN + "/knowledge")
    navigations = []

    def record_navigation(frame):
        if frame == page.main_frame:
            navigations.append(frame.url)

    page.on("framenavigated", record_navigation)
    verify_web_search_controls(page)
    page.get_by_label("Название нового документа").fill(f"Browser snapshot {code}")
    page.get_by_role("button", name="Создать документ").click()
    page.get_by_text("Источник в интернете", exact=True).click()
    page.get_by_label("HTTPS-адрес").fill(address)
    page.locator('.knowledge-upload input[type="file"]').set_input_files({
        "name": "snapshot.txt", "mimeType": "text/plain", "buffer": original.encode(),
    })
    upload = page.get_by_role("button", name="Загрузить", exact=True)
    expect(upload).to_be_disabled()
    page.get_by_label("Дата снимка").fill(captured_on)
    with page.expect_response(
        lambda reply: reply.request.method == "POST" and "/versions?" in reply.url
    ) as admission:
        upload.click()
    version = checked_json(admission.value)["id"]
    expect(page.get_by_text("Ожидает проверки", exact=True)).to_be_visible(timeout=90_000)
    page.get_by_role("button", name="Проверить", exact=True).click()
    expect(page.get_by_role("link", name=address, exact=True)).to_have_attribute("href", address)
    expect(page.locator(".knowledge-review time")).to_have_attribute("datetime", captured_on)
    editor = page.get_by_label("Текст документа для модерации")
    expect(editor).to_have_value(original)
    editor.focus()
    start = original.index(removed)
    end = start + len(removed)
    editor.evaluate("(input, [start, end]) => input.setSelectionRange(start, end)", [start, end])
    editor.press("Delete")
    expect(editor).to_have_value(original.replace(removed, ""))
    editor.press("Control+z")
    expect(editor).to_have_value(original)
    page.get_by_role("button", name="Выбрать весь текст", exact=True).click()
    page.wait_for_function(
        "length => document.activeElement?.selectionStart === 0"
        " && document.activeElement.selectionEnd === length",
        arg=len(original),
    )
    page.keyboard.insert_text(reviewed)
    expect(editor).to_have_value(reviewed)
    approve = page.get_by_role("button", name="Подтвердить и подготовить к публикации")
    expect(approve).to_be_disabled()
    page.get_by_role("checkbox", name="Я проверил редакцию текста", exact=False).check()
    with page.expect_request(
        lambda request: request.method == "POST"
        and urlsplit(request.url).path == f"{KNOWLEDGE}/versions/{version}/approve"
    ) as review_request:
        approve.click()
    assert review_request.value.post_data_json["reviewed_text"] == reviewed
    publish = page.get_by_role("button", name="Опубликовать", exact=True)
    expect(publish).to_be_enabled(timeout=90_000)

    def search(client, headers):
        response = client.post(f"{KNOWLEDGE}/search", json={"query": code}, headers=headers)
        return response.raise_for_status().json()["items"]

    with (
        httpx.Client(base_url=ORIGIN, timeout=15, trust_env=False) as owner,
        httpx.Client(base_url=ORIGIN, timeout=15, trust_env=False) as reader,
    ):
        owner_headers = api_session(owner, probe["account"])
        headers = api_session(reader, probe["reader"])
        assert search(owner, owner_headers) == [], "Draft visible to the curator's search"
        summary_button = page.get_by_role("button", name="Сводка", exact=True)
        summary_button.click()
        summary = page.get_by_role("region", name="Сводка базы знаний")
        published_label = summary.get_by_text("Опубликовано", exact=True)
        published_count = published_label.locator("xpath=..").locator("dd")
        expect(published_count).to_have_text("1")  # The release probe was already published.
        publish.click()
        expect(page.get_by_text("Опубликована", exact=True)).to_be_visible()
        summary.get_by_role("button", name="Обновить сводку").click()
        expect(published_count).to_have_text("2")
        urls = summary.get_by_text("Уникальных веб-адресов в поиске", exact=True)
        expect(urls.locator("xpath=..").locator("dd")).to_have_text("1")
        summary_button.click()
        assert search(reader, headers) == [], "Publication exposed the document without a grant"
        preview = page.get_by_role("button", name="Проверить поиск", exact=True)
        preview.click()
        query = page.get_by_label("Поисковый запрос")
        find = page.get_by_role("button", name="Найти выдержки")
        query.fill(code + "-absent")
        find.click()
        expect(page.get_by_text("Подходящих выдержек нет.", exact=True)).to_be_visible()
        query.fill(code)
        find.click()
        results = page.get_by_role("list", name="Найденные выдержки")
        expect(results).to_contain_text(reviewed)
        expect(results).not_to_contain_text(removed)
        expect(results.get_by_role("link")).to_have_attribute("href", address)
        preview.click()
        page.get_by_role("button", name="Настроить доступ", exact=True).click()
        page.locator(".knowledge-access-form").get_by_label("Логин").fill(probe["reader"]["login"])
        page.get_by_role("button", name="Проверить", exact=True).click()
        answer_access = page.get_by_role(
            "checkbox", name="Использовать разрешённые выдержки в ответах"
        )
        original_access = page.get_by_role("checkbox", name="Скачивать оригинал целиком")
        expect(original_access).not_to_be_checked()
        answer_access.click()
        expect(answer_access).to_be_checked()
        expect(answer_access).to_be_enabled()
        hits = search(reader, headers)
        assert len(hits) == 1 and hits[0]["version_id"] == version
        assert hits[0]["excerpt"] == reviewed
        assert hits[0]["web_source"] == {"url": address, "captured_on": captured_on}
        download = f"{KNOWLEDGE}/versions/{version}/original/download"
        assert reader.post(download, headers=headers).status_code == 404
        assert owner.post(download, headers=owner_headers).raise_for_status().content == (
            original.encode()
        )
        answer_access.click()
        expect(answer_access).not_to_be_checked()
        expect(answer_access).to_be_enabled()
        assert search(reader, headers) == [], "Excerpt remained visible after revocation"
    assert not navigations, "The knowledge workflow navigated or reloaded unexpectedly"
    print("Browser snapshot moderation, publication and answer-only access verified")


def main():
    probe = json.loads((PRIVATE / "probe.json").read_text())
    account = probe["account"]
    with sync_playwright() as session:
        browser = session.chromium.launch()
        page = browser.new_page(viewport={"width": 1365, "height": 900})
        errors = []
        page.on("response", lambda response: print(
            "Browser HTTP failure", response.status, urlsplit(response.url).path,
        ) if response.status >= 400 and response.url.startswith(ORIGIN + "/api/v1/") else None)
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.goto(ORIGIN)
        page.get_by_label("Логин", exact=True).fill(account["login"])
        page.get_by_label("Пароль", exact=True).fill(account["password"])
        page.get_by_role("button", name="Войти", exact=True).click()
        expect(page.locator("button.new-chat")).to_be_enabled(timeout=15000)
        if saved.exists():
            record = json.loads(saved.read_text())
            page.goto(record["url"])
            expect(page.locator(".assistant-message[data-message-id] .prose").filter(
                has_text="Browser acceptance",
            )).to_contain_text(
                "Browser acceptance", timeout=45000
            )
            verify_restored_project_context(page, record["project_message_id"])
            question = "Recovery acceptance"
        else:
            verify_rehearsal_example(page)
            new_chat = page.locator("button.new-chat")
            new_chat.click()
            expect(new_chat).to_be_enabled()
            question = "Browser acceptance"
        page.get_by_role("textbox", name="Сообщение", exact=True).fill(question)
        page.get_by_role("button", name="Отправить сообщение", exact=True).click()
        answer = page.locator(".assistant-message[data-message-id] .prose").filter(
            has_text=question,
        )
        expect(answer).to_contain_text(question, timeout=45000)
        chat_url = page.url
        page.reload()
        expect(answer).to_contain_text(question, timeout=15000)
        if question == "Browser acceptance":
            verify_live_profile_presentation(page)
            verify_request_mode_controls(page)
            verify_saved_design(page)
            source_message_id = verify_design_branch_base(page)
            verify_moderated_snapshot(page, probe)
        assert not errors, "Browser execution errors"
        saved.write_text(json.dumps({
            "url": chat_url, "project_message_id": (
                source_message_id if question == "Browser acceptance"
                else record["project_message_id"]
            ),
        }), encoding="utf-8")
        browser.close()
    print("Browser login, completion and reload verified")


if __name__ == "__main__":
    main()
