"""Offline lexical retrieval over approved units; no raw-document disclosure."""

import hashlib
import re
from dataclasses import dataclass
from uuid import UUID, uuid5

from partner_agent.documents.canonical import CanonicalDocument, Locator

TOKEN = re.compile(r"[\w]+(?:[.,/-][\w]+)*", re.UNICODE)
# Function words alone cannot support a search result for a new question.
STOP_WORDS = frozenset(
    {
        "а",
        "был",
        "была",
        "быть",
        "в",
        "во",
        "где",
        "да",
        "для",
        "до",
        "за",
        "и",
        "из",
        "или",
        "как",
        "какая",
        "какие",
        "какой",
        "когда",
        "кто",
        "ли",
        "мне",
        "на",
        "о",
        "об",
        "от",
        "по",
        "под",
        "при",
        "про",
        "с",
        "со",
        "у",
        "что",
        "это",
        "a",
        "an",
        "and",
        "at",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "with",
    }
)
GENERIC_IDENTIFIER_WORDS = frozenset({"код", "code", "идентификатор", "identifier"})


def query_terms(query: str) -> tuple[set[str], set[str]]:
    """Preserve exact numeric/code tokens without assuming a partner vocabulary."""
    terms = set(TOKEN.findall(query.casefold())) - STOP_WORDS
    return terms, {term for term in terms if any(char.isdigit() for char in term)}


def matched_terms(terms: set[str], identifiers: set[str], text: str) -> set[str]:
    tokens = set(TOKEN.findall(text.casefold()))
    if identifiers and not identifiers.intersection(tokens):
        return set()
    matches = terms.intersection(tokens)
    # A request for a specific kind of code needs at least one matching qualifier.
    if terms - GENERIC_IDENTIFIER_WORDS and matches <= GENERIC_IDENTIFIER_WORDS:
        return set()
    return matches


@dataclass(frozen=True)
class ApprovedUnit:
    id: UUID
    document_id: UUID
    version_id: UUID
    block_id: UUID
    text: str
    locator: Locator
    policy_revision: int
    allowed_users: frozenset[UUID]
    denied_users: frozenset[UUID] = frozenset()


@dataclass(frozen=True)
class Chunk:
    id: UUID
    unit: ApprovedUnit
    text: str
    text_sha256: str
    offset: int
    chunker_version: str = "bounded-chars-v1"


@dataclass(frozen=True)
class ActivePolicy:
    version_id: UUID
    revision: int
    published: bool


@dataclass(frozen=True)
class Evidence:
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    block_id: UUID
    excerpt: str
    locator: Locator
    score: float


def approve_blocks(
    canonical: CanonicalDocument,
    block_ids: frozenset[UUID],
    allowed_users: frozenset[UUID],
    policy_revision: int,
) -> tuple[ApprovedUnit, ...]:
    if policy_revision < 1:
        raise ValueError("invalid_policy_revision")
    known = {block.id for block in canonical.blocks}
    if not block_ids <= known:
        raise ValueError("unknown_disclosure_block")
    return tuple(
        ApprovedUnit(
            id=uuid5(canonical.version_id, f"approved:{block.id}:{policy_revision}"),
            document_id=canonical.document_id,
            version_id=canonical.version_id,
            block_id=block.id,
            text=block.text,
            locator=block.locator,
            policy_revision=policy_revision,
            allowed_users=allowed_users,
        )
        for block in canonical.blocks
        if block.id in block_ids
    )


def chunk_unit(unit: ApprovedUnit, max_chars: int = 1600, overlap: int = 160) -> tuple[Chunk, ...]:
    if max_chars < 1 or overlap < 0 or overlap >= max_chars:
        raise ValueError("invalid_chunk_bounds")
    chunks = []
    start = 0
    while start < len(unit.text):
        end = min(start + max_chars, len(unit.text))
        text = unit.text[start:end]
        digest = hashlib.sha256(text.encode()).hexdigest()
        chunks.append(
            Chunk(
                id=uuid5(unit.id, f"bounded-chars-v1:{max_chars}:{overlap}:{start}:{digest}"),
                unit=unit,
                text=text,
                text_sha256=digest,
                offset=start,
            )
        )
        if end == len(unit.text):
            break
        start = end - overlap
    return tuple(chunks)


def search(
    query: str,
    user_id: UUID,
    chunks: tuple[Chunk, ...],
    policies: dict[UUID, ActivePolicy],
    limit: int = 8,
) -> tuple[Evidence, ...]:
    from partner_agent.retrieval.ranking import rank_scores

    if not 1 <= limit <= 100 or len(query.encode()) > 32768:
        raise ValueError("invalid_search_bounds")
    terms, _ = query_terms(query)
    if not terms:
        return ()
    eligible = []
    for chunk in chunks:
        unit = chunk.unit
        policy = policies.get(unit.document_id)
        if (
            policy is None
            or not policy.published
            or policy.version_id != unit.version_id
            or policy.revision != unit.policy_revision
            or user_id not in unit.allowed_users
            or user_id in unit.denied_users
        ):
            continue
        eligible.append(chunk)
    scores = rank_scores(query, [chunk.text for chunk in eligible])
    found = []
    for chunk, score in zip(eligible, scores, strict=True):
        if not score:
            continue
        unit = chunk.unit
        found.append(
            Evidence(
                chunk_id=chunk.id,
                document_id=unit.document_id,
                version_id=unit.version_id,
                block_id=unit.block_id,
                excerpt=chunk.text,
                locator=unit.locator,
                score=score,
            )
        )
    return tuple(sorted(found, key=lambda hit: (-hit.score, str(hit.chunk_id)))[:limit])
