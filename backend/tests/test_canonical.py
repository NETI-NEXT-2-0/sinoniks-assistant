from uuid import uuid4

import pytest

from partner_agent.documents.canonical import CanonicalError, extract_utf8


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
