import pytest

from partner_agent.documents.quality import has_mixed_script_identifier


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Код КРI-2036", True),  # Cyrillic КР and Latin I.
        ("Код AB-123 и АВ-456", False),  # Each token uses one script.
        ("Обучение Python 2026", False),
        ("Python-курс", False),  # Mixed scripts, but no numeric identifier.
        ("тест АB_12", True),
        ("Ставка 12,5%", False),
    ],
)
def test_mixed_script_identifier_is_a_hint_not_a_correction(text, expected):
    assert has_mixed_script_identifier(text) is expected
