from uuid import uuid4

from partner_agent.documents.canonical import extract_utf8
from partner_agent.documents.chat_files import chunk_digest, extract_chunks
from partner_agent.runtime.rag_provider import build_messages


def test_private_chunks_preserve_exact_source_substrings_and_bounds():
    source = "д" * 4001
    canonical = extract_utf8(source.encode(), uuid4(), uuid4(), "text/plain")
    chunks = extract_chunks(canonical)
    assert "".join(item["text"] for item in chunks) == source
    assert all(len(item["text"]) <= 1400 for item in chunks)
    assert len(chunk_digest(chunks)) == 64
    changed = [{**chunks[0], "text": "changed"}, *chunks[1:]]
    assert chunk_digest(changed) != chunk_digest(chunks)


def test_prompt_allows_small_talk_but_keeps_document_specific_answers_grounded():
    instructions = build_messages("Как дела?", [])[0]["content"]
    assert "small talk" in instructions and "general knowledge" in instructions
    assert "organization-specific facts" in instructions
    assert "only the supplied evidence" in instructions
    assert "do not invent document facts" in instructions
