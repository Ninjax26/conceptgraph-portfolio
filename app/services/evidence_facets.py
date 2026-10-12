"""Conservative, bounded decomposition; no LLM or inferred question rewriting."""
import re


def comparison_facets(question: str) -> list[str]:
    text = question.strip()
    # Qualifiers or nested comparisons are ambiguous; keep the full question.
    if any(character in text for character in (":", ";", "\n")):
        return []
    match = re.fullmatch(r"(?:Compare|Contrast) (.+?) with (.+?)[.?]?", text, re.IGNORECASE)
    if not match or len(re.findall(r"\bwith\b", text, re.IGNORECASE)) != 1:
        return []
    return list(match.groups())
