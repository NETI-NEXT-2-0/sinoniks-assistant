from uuid import uuid4

import pytest

from partner_agent.documents.canonical import CanonicalError, extract_utf8
from partner_agent.documents.edited_copy import MARKDOWN_LOCATORS, ReviewedCopy, render_copy


def test_utf8_locators_point_to_original_russian_source_and_ids_are_stable():
    source = "\ufeffРаздел 1\r\n\r\nСтавка 12,5% — примечание¹\n"
    document, version = uuid4(), uuid4()
    result = extract_utf8(source.encode(), document, version, "text/plain")
    assert result == extract_utf8(source.encode(), document, version, "text/plain")
    assert [block.locator.line for block in result.blocks] == [1, 3]
    for block in result.blocks:
        assert source[block.locator.char_start : block.locator.char_end] == block.text
    other = extract_utf8(source.encode(), document, uuid4(), "text/plain")
    assert {b.id for b in result.blocks}.isdisjoint(b.id for b in other.blocks)
    assert result.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    ("data", "media", "code"),
    [
        (b"\xff", "text/plain", "invalid_utf8"),
        (b"secret\x00", "text/plain", "binary_content"),
        (b" \r\n", "text/plain", "empty_document"),
        (b"secret", "application/pdf", "unsupported_media_type"),
    ],
)
def test_rejected_source_has_safe_technical_error(data, media, code):
    with pytest.raises(CanonicalError, match=f"^{code}$"):
        extract_utf8(data, uuid4(), uuid4(), media)


def test_stored_text_canonical_rejects_changed_content_or_locator():
    from pydantic import ValidationError

    canonical = extract_utf8(b"Public\nPrivate", uuid4(), uuid4(), "text/plain")
    changed = canonical.model_dump(mode="json")
    changed["blocks"][0]["text"] = "Changed"
    with pytest.raises(ValidationError):
        type(canonical).model_validate(changed)
    changed = canonical.model_dump(mode="json")
    changed["blocks"][1]["locator"]["line"] = 1
    with pytest.raises(ValidationError):
        type(canonical).model_validate(changed)


def test_reviewed_markdown_preserves_sections_offsets_and_fenced_examples():
    text = (
        "Введение\r\n\r\n# Сетевой обмен ##\r\nОписание правил\r\n"
        + ("```text\r\n# Пример команды\r\n" + "деталь " * 300 + "\r\n```\r\n")
        + "## Файлы\r\nМетоды передачи\r\n"
    )
    copy = ReviewedCopy(
        document_id=uuid4(), version_id=uuid4(), canonical_sha256="a" * 64,
        text=text, locator_profile=MARKDOWN_LOCATORS,
    )
    units = render_copy(copy, "b" * 64)
    assert "".join(unit[1] for unit in units) == text
    assert [unit[3].get("section") for unit in units] == [
        None, "Сетевой обмен", "Сетевой обмен", "Файлы",
    ]
    for _, fragment, _, locator in units:
        assert text[locator["start"]:locator["end"]] == fragment
    assert render_copy(ReviewedCopy.model_validate_json(copy.model_dump_json()), "b" * 64) == units


def test_older_reviewed_copy_keeps_original_format_and_ranges():
    copy = ReviewedCopy(
        document_id=uuid4(), version_id=uuid4(), canonical_sha256="a" * 64,
        text="Введение\n# Раздел\nТекст",
    )
    assert "locator_profile" not in copy.model_dump_json()
    (_, fragment, _, locator), = render_copy(copy, "b" * 64)
    assert fragment == copy.text
    assert "section" not in locator and locator["start"] == 0 and locator["end"] == len(copy.text)
