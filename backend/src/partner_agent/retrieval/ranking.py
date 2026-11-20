"""Request-local BM25 over already authorized units; no corpus or token cache."""

import math
import re
from collections import Counter
from typing import Any

from snowballstemmer import stemmer  # type: ignore[import-untyped]

from partner_agent.retrieval.lexical import GENERIC_IDENTIFIER_WORDS, TOKEN, query_terms

PROFILE = "lexical_bm25_ru_v1"
RUSSIAN = re.compile(r"[а-яё]{4,}")


def rank_scores(query: str, texts: list[str]) -> list[float]:
    """Score without using inaccessible documents in document-frequency statistics."""
    terms, identifiers = query_terms(query)
    if not terms or not texts:
        return [0.0] * len(texts)
    # Snowball objects are mutable and not thread-safe. One instance per invocation.
    russian: Any = stemmer("russian")
    local_tokens: dict[str, str] = {}

    def normalize(term: str) -> str:
        if not RUSSIAN.fullmatch(term):
            return term
        if term in local_tokens:
            return local_tokens[term]
        normalized_term = str(russian.stemWord(term))
        if len(local_tokens) < 50_000:
            local_tokens[term] = normalized_term
        return normalized_term

    normalized = {normalize(term) for term in terms}
    generic = {normalize(term) for term in GENERIC_IDENTIFIER_WORDS}
    counts: list[Counter[str]] = []
    lengths: list[int] = []
    allowed: list[bool] = []
    frequencies: Counter[str] = Counter()
    for text in texts:
        tokens = TOKEN.findall(text.casefold())
        # Exact numeric/code matching happens BEFORE stemming, including mixed scripts.
        valid = not identifiers or bool(identifiers.intersection(tokens))
        counts_for_query: Counter[str] = Counter()
        for token in tokens:
            token = normalize(token)
            if token in normalized:
                counts_for_query[token] += 1
        matches = set(counts_for_query)
        if normalized - generic and matches <= generic:
            valid = False
        allowed.append(valid)
        counts.append(counts_for_query)
        lengths.append(len(tokens))
        frequencies.update(matches)
    average = sum(lengths) / len(lengths) or 1.0
    idfs = {
        term: math.log1p((len(texts) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
        for term in normalized
    }
    result = []
    for permitted, count, length in zip(allowed, counts, lengths, strict=True):
        score = 0.0
        if permitted:
            for term, frequency in sorted(count.items()):
                score += (
                    idfs[term]
                    * frequency
                    * 2.2
                    / (frequency + 1.2 * (0.25 + 0.75 * length / average))
                )
        # A bounded relevance score, never a probability/confidence in the answer.
        result.append(score / (1 + score))
    return result
