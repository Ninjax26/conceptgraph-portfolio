"""Mechanical answer/reference checks, not claim support or semantic accuracy."""
import re
from typing import Any


REFUSAL_TEXT = "I could not find enough reliable course content to answer this confidently."
_REFERENCE = re.compile(r"\[\s*Source\b[^\]\n]*(?:\]|(?=\n|$))", re.I)
_LABEL = re.compile(r"\[Source\s+([0-9]{1,9})(?:,\s*p\.\s*([0-9]{1,9}))?\]", re.I)


def citation_references(answer: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs = []
    for token in _REFERENCE.finditer(answer):
        # Escaped labels in quoted evidence are literal source text. An even
        # number of backslashes still leaves an active Markdown label.
        prefix = answer[:token.start()]
        if (len(prefix) - len(prefix.rstrip('\\'))) % 2:
            continue
        tag = token.group()
        line_start = answer.rfind("\n", 0, token.start()) + 1
        line_prefix = answer[line_start:token.start()]
        # A Markdown definition has no visible label/body; it can also turn
        # matching labels elsewhere into links to an unverified destination.
        definition = (re.fullmatch(r"[\t >*+\-0-9.)]*", line_prefix) is not None
                      and re.match(r"[ \t]*:", answer[token.end():]) is not None)
        match = None if definition else _LABEL.fullmatch(tag)
        index, page = (int(match[1]), int(match[2]) if match[2] else None) if match else (None, None)
        in_range = index is not None and 1 <= index <= len(sources)
        actual_page = sources[index - 1].get("page_number") if in_range else None
        page_ok = in_range and (page is None or (type(actual_page) is int and actual_page > 0 and page == actual_page))
        refs.append({"label": tag, "index": index, "index_valid": in_range, "page_valid": page_ok})
    return refs


def answer_contract_error(answer: str, question: str, sources: list[dict[str, Any]] | None = None) -> str | None:
    if not isinstance(answer, str) or not answer.strip():
        return "The provider returned an empty answer."
    normalize = lambda value: re.sub(r"\W+", "", value.casefold())
    if normalize(answer) == normalize(question):
        return "The provider returned only the question."
    # Preserve private two-argument callers. All provider routes supply sources.
    if sources is None:
        return None
    if " ".join(answer.split()).casefold() == REFUSAL_TEXT.casefold():
        return None
    refs = citation_references(answer, sources)
    if not refs:
        return "The provider returned an answer without source citations."
    if any(not ref["index_valid"] or not ref["page_valid"] for ref in refs):
        return "The provider returned an invalid source or page citation."
    if any(not str(sources[ref["index"] - 1].get("supporting_passage") or "").strip() for ref in refs):
        return "The provider cited a source without a supporting passage."
    body = normalize(_REFERENCE.sub("", answer))
    if not body:
        return "The provider returned only citations."
    if body == normalize(question):
        return "The provider returned only the question with citations."
    return None
