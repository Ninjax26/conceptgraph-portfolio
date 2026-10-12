"""Bounded exact-identifier lookup and rank fusion alongside dense retrieval."""

import re
from collections.abc import Sequence
from typing import Any


IDENTIFIER = re.compile(
    r"\b[A-Z]{2,5}\s+[0-9]+(?:\.[0-9]+)*\b|"
    r"\b(?=[A-Za-z0-9_.-]*[0-9])[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*\b"
    r"|\b\d+\.\d+(?:\.\d+)*\b|\b\d{3,}\b"
)
DASHES = str.maketrans({character: "-" for character in "\u2010\u2011\u2012\u2013\u2014\u2212"})
EVIDENCE_PASSAGE_LIMIT = 900


def identifier_terms(text: str, limit: int = 64, *, named_only: bool = False) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    if limit <= 0:
        return result
    for match in IDENTIFIER.finditer(text.translate(DASHES)):
        term = re.sub(r"[-_\s]+", "", match.group().casefold())
        if term in seen or (named_only and not re.match(r"[a-z]{2,}", term)):
            continue
        seen.add(term)
        result.append(term)
        if len(result) == limit:
            break
    return result


def _identifier_pattern(identifier: str) -> str:
    fragments = []
    for part in re.split(r"[-_]+", identifier.translate(DASHES)):
        escaped = re.escape(part)
        fragments.append(re.sub(r"(?<=[A-Za-z])(?=[0-9])", lambda _: r"[-_\s]*", escaped))
    pattern = r"[-_\s]*".join(fragments)
    return r"(?<![\w.-])" + pattern + r"(?!\w|[-_.][A-Za-z0-9])"


def identifier_supported(identifier: str, text: str) -> bool:
    """Case/separator variations only; never match another identifier's prefix."""
    return bool(re.search(_identifier_pattern(identifier), text.translate(DASHES), flags=re.IGNORECASE))


def passage_excerpt(passage: str, question: str | None, limit: int = EVIDENCE_PASSAGE_LIMIT) -> str:
    """A literal, bounded window for reranking and citation; never rewrites evidence."""
    if len(passage) <= limit or not question:
        return passage[:limit]
    stopwords = {"what", "which", "when", "where", "does", "with", "from", "that", "this", "should", "could", "would", "the", "and", "for", "how", "why", "explain", "describe", "please"}
    terms = {word.casefold() for word in re.findall(r"\w+", question) if len(word) >= 3 and word.casefold() not in stopwords}
    identifiers = identifier_terms(question, limit=8)
    matches = [match.start() for match in re.finditer(r"\w+", passage) if match.group().casefold() in terms]
    matches += [match.start() for term in identifiers for match in re.finditer(_identifier_pattern(term), passage.translate(DASHES), flags=re.IGNORECASE)]
    if not matches:
        return passage[:limit]
    positions = sorted({max(0, min(position - 150, len(passage) - limit)) for position in matches})
    if len(positions) > 32:
        positions = [positions[round(index * (len(positions) - 1) / 31)] for index in range(32)]
    def score(start):
        window = passage[start:start + limit]
        return len(terms & {word.casefold() for word in re.findall(r"\w+", window)}) + 2 * sum(identifier_supported(term, window) for term in identifiers)
    start = max(positions, key=score)
    end = min(len(passage), start + limit)
    if start and passage[start - 1].isalnum():
        boundary = passage.find(" ", start, end)
        start = boundary + 1 if boundary >= 0 else start
    if end < len(passage) and passage[end].isalnum():
        boundary = passage.rfind(" ", start, end)
        end = boundary if boundary > start else end
    return passage[start:end]


def rerank_passages(passage: str, question: str) -> list[str]:
    """Keep whole-passage context plus at most two literal sentence windows.

    A relevant sentence near the end of a mixed-topic chunk must not be hidden
    by its unrelated introduction. Windows overlap to retain local context;
    they are never summaries or new evidence.
    """
    text = " ".join(passage.split())
    # Score the same bounded text that citations and generation will receive;
    # otherwise a later trim can remove the reason for a high relevance score.
    whole = passage_excerpt(text, question, EVIDENCE_PASSAGE_LIMIT)
    identifiers = identifier_terms(question, limit=8, named_only=True)
    if identifiers and identifier_terms(text, named_only=True) and not any(
        identifier_supported(term, text) for term in identifiers
    ):
        # Still rank the original candidate, but do not spend extra work on
        # another equipment identifier that the evidence guard will exclude.
        return [whole]
    sentences = list(re.finditer(r"\S.*?(?:[.!?](?=\s|$)|$)", text))
    if len(sentences) < 3:
        return [whole]
    windows = []
    for index in range(len(sentences) - 1):
        window = text[sentences[index].start():sentences[min(index + 1, len(sentences) - 1)].end()].strip()
        if window and len(window) <= EVIDENCE_PASSAGE_LIMIT and window != whole:
            windows.append(window)
    stopwords = {
        "what", "which", "when", "where", "does", "with", "from", "that", "this",
        "should", "could", "would", "the", "and", "for", "how", "why", "explain",
        "describe", "please", "must", "its", "can", "are", "not",
    }
    terms = {word.casefold() for word in re.findall(r"\w+", question)
             if len(word) >= 3 and word.casefold() not in stopwords}
    def focus_score(window: str) -> int:
        return (
            2 * sum(identifier_supported(term, window) for term in identifiers)
            + len(terms & set(re.findall(r"\w+", window.casefold())))
        )

    # Do not spend extra reranker work on windows matching only generic text.
    windows = [window for window in windows if focus_score(window) >= 2]
    windows.sort(key=focus_score, reverse=True)
    return list(dict.fromkeys([whole, *windows[:2]]))


def fuse_candidates(
    dense: Sequence[dict[str, Any]], lexical: Sequence[dict[str, Any]], limit: int,
) -> list[dict[str, Any]]:
    """Reciprocal rank fusion; its score never substitutes for cosine similarity."""
    candidates: dict[str, dict[str, Any]] = {}
    ranks: dict[str, float] = {}
    for ranking in (dense, lexical):
        seen: set[str] = set()
        for rank, chunk in enumerate(ranking, start=1):
            key = str(chunk["id"])
            if key in seen:
                continue
            seen.add(key)
            candidates.setdefault(key, dict(chunk))
            ranks[key] = ranks.get(key, 0.0) + 1 / (60 + rank)
    return [dict(candidates[key], fusion_score=ranks[key])
            for key in sorted(ranks, key=lambda key: (-ranks[key], key))[:limit]]
