"""Controlled conversational outcomes; never expose rejected model output."""


def recovery_message(code: str) -> str | None:
    if code == "rag_context_limit":
        return (
            "Проект вместе с запросом превышает доступное окно модели. "
            "Сохранённые условия не изменились. Отправьте короткую адресную правку "
            "или откройте редактор проекта."
        )
    if code in {"provider_request_cap_exceeded", "provider_daily_cap_exceeded"}:
        return (
            "Достигнут заданный лимит расходов. Сохранённый проект доступен. "
            "Продолжить можно после изменения лимита администратором."
        )
    if code in {
        "provider_request_timeout",
        "provider_request_failed",
        "provider_stream_failed",
        "provider_route_required",
        "provider_incomplete_response",
        "runtime_timeout",
    }:
        return (
            "Сервис ответов сейчас недоступен. Попробуйте повторить вопрос чуть позже. "
            "Изменения в проект не вносились."
        )
    if code.startswith("provider_invalid_design") or code in {
        "provider_request_mode_violation",
        "request_mode_violation",
    }:
        return (
            "Не удалось однозначно подготовить изменение схемы. Уточните, пожалуйста, "
            "какую сеть, устройство или передачу данных нужно добавить или изменить. "
            "Изменения в проект не вносились."
        )
    if code in {
        "provider_unauthorized_citation",
        "provider_missing_grounded_citation",
        "provider_invalid_citation_quote",
        "provider_invalid_citation_source",
    }:
        return (
            "Не удалось подтвердить ответ по доступным источникам. Уточните, пожалуйста, "
            "о какой функции, протоколе или части схемы вы спрашиваете."
        )
    if code.startswith("provider_invalid_") or code in {
        "provider_response_encoding",
        "provider_response_too_large",
    }:
        return (
            "Не удалось надёжно разобрать ответ. Уточните, пожалуйста, вопрос "
            "или нужное изменение схемы. Изменения в проект не вносились."
        )
    return None
