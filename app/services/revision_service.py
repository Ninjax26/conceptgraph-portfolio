"""Compare the text of two original PDFs without inference or embedding calls."""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import unified_diff
from hashlib import sha256
import re

import pymupdf

from app.core.config import settings
from app.services.document_layout import ordered_lines
from app.services.parser_service import ParserService


class ComparisonUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Section:
    id: str
    page: int
    heading: str
    text: str


def extract_sections(content: bytes, expected_hash: str | None) -> tuple[list[Section], list[int]]:
    if expected_hash and sha256(content).hexdigest() != expected_hash:
        raise ComparisonUnavailable("The stored PDF differs from the uploaded revision.")
    if len(content) > settings.max_pdf_size_mb * 1024 * 1024:
        raise ComparisonUnavailable("The source exceeds the configured PDF size limit.")
    result: list[Section] = []
    review_pages = []
    text_size = 0
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        if pdf.needs_pass or not 0 < len(pdf) <= settings.max_pages_per_document:
            raise ComparisonUnavailable("The source is locked, empty or exceeds the configured page limit.")
        for index, page in enumerate(pdf, 1):
            layout = page.get_text("dict", sort=True, flags=pymupdf.TEXTFLAGS_TEXT)
            lines = ordered_lines(page, ParserService._layout_lines_from_dict(layout, page.cropbox.height))
            # Image-only pages, mixed diagrams and low-quality text can hide changes.
            page_text = "\n".join(line.text for line in lines)
            if (not lines or page.get_image_info()
                    or ParserService._text_quality(page_text).score < ParserService.MIN_TEXT_QUALITY):
                review_pages.append(index)
            text_size += len(page_text)
            if text_size > 250_000:
                raise ComparisonUnavailable("A revision exceeds the comparison limit of 250,000 characters.")
            body_size = ParserService._body_font_size(lines)
            heading = ""
            body = []
            page_sections = []
            for i, line in enumerate(lines):
                score = ParserService._layout_heading_score(
                    line, body_font_size=body_size, previous=lines[i - 1] if i else None,
                    following=lines[i + 1] if i + 1 < len(lines) else None,
                )
                if score >= 4 and line.kind != "table_row":
                    if heading or body:
                        page_sections.append((heading, "\n".join(body)))
                    heading, body = line.text, []
                else:
                    body.append(line.text)
            if heading or body:
                page_sections.append((heading, "\n".join(body)))
            for ordinal, (title, text) in enumerate(page_sections, 1):
                result.append(Section(f"{index}.{ordinal}", index, title, text))
            if len(result) > 240:
                raise ComparisonUnavailable("A revision exceeds the comparison limit of 240 sections.")
    return result, review_pages


def _by_key(sections, key):
    groups = defaultdict(list)
    for section in sections:
        value = key(section)
        if value:
            groups[value].append(section)
    return groups


def _tokens(section):
    return set(re.findall(r"\w+", f"{section.heading} {section.text}".casefold()))


def compare_sections(earlier: list[Section], later: list[Section], decisions: dict[str, str | None]):
    """A human decision can only resolve an unmatched source; matches are one-to-one."""
    left = {section.id: section for section in earlier}
    right = {section.id: section for section in later}
    matches = {}
    for key in (lambda s: " ".join(s.heading.casefold().split()), lambda s: s.text.strip()):
        left_groups, right_groups = _by_key(left.values(), key), _by_key(right.values(), key)
        for value, group in left_groups.items():
            targets = right_groups.get(value, [])
            if len(group) == len(targets) == 1:
                old, new = group[0], targets[0]
                matches[old.id] = (new, "automatic")
                del left[old.id], right[new.id]
    for old_id, new_id in decisions.items():
        if old_id not in left or (new_id is not None and new_id not in right):
            raise ComparisonUnavailable("A review refers to an unavailable or already matched section.")
        matches[old_id] = (right.pop(new_id) if new_id is not None else None, "reviewed")
        del left[old_id]

    changes, pending = [], []
    counts = Counter()
    right_tokens = {item.id: _tokens(item) for item in right.values()}
    for old in earlier:
        if old.id in left and right:
            old_tokens = _tokens(old)
            def score(item):
                tokens = right_tokens[item.id]
                return len(tokens & old_tokens) / max(1, len(tokens | old_tokens))
            ordered = sorted(right.values(), key=score, reverse=True)
            pending.append({"earlier": asdict(old), "suggested": ordered[0].id if score(ordered[0]) else None})
            continue
        new, basis = matches.get(old.id, (None, "unpaired"))
        if new is None:
            kind = "removed"
        elif old.text != new.text:
            kind = "modified"
        elif old.heading != new.heading:
            kind = "renamed"
        elif old.page != new.page:
            kind = "moved"
        else:
            kind = "unchanged"
        counts[kind] += 1
        if kind != "unchanged":
            changes.append(_change(kind, basis, old, new))
    if not pending:
        for new in right.values():
            counts["added"] += 1
            changes.append(_change("added", "unpaired", None, new))
    return {"counts": dict(counts), "changes": changes, "pending": pending,
            "candidates": [asdict(s) for s in right.values()] if pending else []}


def _change(kind, basis, old, new):
    def lines(section):
        if section is None:
            return []
        return ([section.heading] if section.heading else []) + section.text.splitlines()
    # The UI renders these exact lines with both source PDF page links.
    diff = list(unified_diff(lines(old), lines(new), n=1, lineterm=""))[2:]
    return {"kind": kind, "basis": basis, "earlier": asdict(old) if old else None,
            "later": asdict(new) if new else None, "diff": diff}
