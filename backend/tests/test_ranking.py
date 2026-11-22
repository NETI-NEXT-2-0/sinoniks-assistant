from concurrent.futures import ThreadPoolExecutor

import pytest

from partner_agent.retrieval.ranking import rank_scores


@pytest.mark.parametrize(
    ("query", "text"),
    [
        ("проверки файлов", "Проверка файла выполняется антивирусом."),
        ("политики доступа", "Политика доступом не определяется."),
        ("журналы событий", "Журнал события доступен администратору."),
        ("передача данных", "Передачей данными управляет оператор."),
    ],
)
def test_russian_word_forms_match_without_rewriting_excerpts(query: str, text: str) -> None:
    assert rank_scores(query, [text])[0] > 0
    assert isinstance(text, str)


@pytest.mark.parametrize(
    ("query", "wrong", "right"),
    [
        ("курс EDU-204", "курс EDU-2040", "курс EDU-204"),
        ("курс EDU-204", "курс ЕDU-204", "курс EDU-204"),
        ("ставка 12,5%", "ставка 12,6%", "ставка 12,5%"),
        ("Modbus TCP 502", "Modbus TCP 503", "Modbus TCP 502"),
        ("версия 1.9", "версия 1.8", "версия 1.9"),
    ],
)
def test_numbers_and_codes_remain_exact(query: str, wrong: str, right: str) -> None:
    scores = rank_scores(query, [wrong, right])
    assert scores[0] == 0 and scores[1] > 0


def test_multiple_identifiers_are_alternatives_not_silent_prefixes() -> None:
    scores = rank_scores("курс EDU-204 и EDU-205", ["курс EDU-204", "курс EDU-205", "EDU-2040"])
    assert scores[0] > 0 and scores[1] > 0 and scores[2] == 0


def test_generic_code_word_cannot_support_unrelated_code_answer() -> None:
    assert rank_scores("Резервный код организатора", ["Код курса RAG-204"]) == [0]
    assert rank_scores("код", ["Код курса RAG-204"])[0] > 0
    assert rank_scores("Льготы для ветеранов", ["Для входа нужен паспорт"]) == [0]


def test_rarity_and_length_break_raw_overlap_ties() -> None:
    texts = ["есть встроенный антивирус"] + ["есть " + "обычный текст " * 50 for _ in range(10)]
    scores = rank_scores("Есть ли встроенный антивирус?", texts)
    assert scores[0] > max(scores[1:])
    assert all(0 <= score < 1 for score in scores)


def test_same_request_is_deterministic_and_order_independent() -> None:
    texts = ["Проверка файлов ICAP", "Проверки файла", "Другой документ"]
    expected = rank_scores("проверка файла", texts)
    assert rank_scores("проверка файла", texts) == expected
    assert rank_scores("проверка файла", list(reversed(texts))) == list(reversed(expected))


def test_russian_stemmer_is_not_shared_across_threads() -> None:
    texts = ["проверки файлов", "файловая система", "журнал событий"]
    expected = rank_scores("проверка файла", texts)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert (
            list(pool.map(lambda _: rank_scores("проверка файла", texts), range(12)))
            == [expected] * 12
        )


def test_no_english_fuzzy_matching_or_empty_query_candidates() -> None:
    scores = rank_scores("Python", ["Pythonic", "Python"])
    assert scores[0] == 0 and scores[1] > 0
    assert rank_scores("и для в", ["для всех"]) == [0]
    assert rank_scores("files", []) == []
