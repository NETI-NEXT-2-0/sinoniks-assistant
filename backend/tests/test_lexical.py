from dataclasses import replace
from uuid import uuid4

from partner_agent.documents.canonical import extract_utf8
from partner_agent.retrieval.lexical import ActivePolicy, approve_blocks, chunk_unit, search


def test_approved_excerpt_only_and_live_policy_revocation():
    user, other = uuid4(), uuid4()
    canonical = extract_utf8(
        "Код договора AB-123 ставка 12,5%\nСекретный пароль".encode(),
        uuid4(),
        uuid4(),
        "text/plain",
    )
    units = approve_blocks(canonical, frozenset([canonical.blocks[0].id]), frozenset([user]), 1)
    chunks = chunk_unit(units[0])
    policies = {canonical.document_id: ActivePolicy(canonical.version_id, 1, True)}
    hits = search("AB-123", user, chunks, policies)
    assert len(hits) == 1 and hits[0].block_id == canonical.blocks[0].id
    assert not search("Секретный", user, chunks, policies)
    assert not search("AB-12", user, chunks, policies)
    assert not search("AB-123", other, chunks, policies)
    assert not search(
        "AB-123",
        user,
        tuple(replace(c, unit=replace(c.unit, denied_users=frozenset([user]))) for c in chunks),
        policies,
    )
    for policy in (
        ActivePolicy(canonical.version_id, 2, True),
        ActivePolicy(uuid4(), 1, True),
        ActivePolicy(canonical.version_id, 1, False),
    ):
        assert not search("AB-123", user, chunks, {canonical.document_id: policy})


def test_chunks_are_bounded_stable_and_preserve_full_approved_text():
    user = uuid4()
    canonical = extract_utf8(("Текст " * 100).encode(), uuid4(), uuid4(), "text/plain")
    unit = approve_blocks(canonical, frozenset([canonical.blocks[0].id]), frozenset([user]), 1)[0]
    chunks = chunk_unit(unit, max_chars=50, overlap=10)
    assert chunks == chunk_unit(unit, max_chars=50, overlap=10)
    assert len(set(c.id for c in chunks)) == len(chunks)
    assert all(
        len(c.text) <= 50 and unit.text[c.offset : c.offset + len(c.text)] == c.text for c in chunks
    )
    assert chunks[0].offset == 0
    assert chunks[-1].offset + len(chunks[-1].text) == len(unit.text)


def test_numeric_identifiers_are_exact_and_multiple_codes_can_match_separate_units():
    from partner_agent.retrieval.lexical import matched_terms, query_terms

    terms, identifiers = query_terms("курс EDU-204 и EDU-205")
    assert not matched_terms(terms, identifiers, "курс EDU-2040")
    assert "edu-204" in matched_terms(terms, identifiers, "курс EDU-204")
    assert "edu-205" in matched_terms(terms, identifiers, "курс EDU-205")
    assert not matched_terms(terms, identifiers, "курс ЕDU-204")  # Cyrillic Е, not Latin E.
    amount_terms, amounts = query_terms("ставка 12,5%")
    assert not matched_terms(amount_terms, amounts, "ставка 12,6%")
    assert "12,5" in matched_terms(amount_terms, amounts, "ставка 12,5%")
    plain_terms, none = query_terms("курс Python")
    assert matched_terms(plain_terms, none, "курс Java") == {"курс"}


def test_generic_words_do_not_match_an_unrelated_document():
    from partner_agent.retrieval.lexical import matched_terms, query_terms

    assert not matched_terms(*query_terms("Льготы для ветеранов"), "Для входа нужен паспорт")
    assert not matched_terms(*query_terms("Резервный код организатора"), "Код курса RAG-204")
    assert matched_terms(*query_terms("Резервный код"), "Резервный код STAFF-731")
    assert matched_terms(*query_terms("Код"), "Код курса RAG-204") == {"код"}
