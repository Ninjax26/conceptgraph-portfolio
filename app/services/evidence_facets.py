"""Conservative, bounded decomposition; no LLM or inferred question rewriting."""
import re


_SCOPE_MODIFIER = re.compile(
    r"\b(?:for|in|on|with|under|after|before|during|since|until|within|outside|using|over|by|at)\b",
    re.IGNORECASE,
)


def comparison_facets(question: str) -> list[str]:
    text = question.strip()
    # Qualifiers or nested comparisons are ambiguous; keep the full question.
    if any(character in text for character in (":", ";", "\n")):
        return []
    match = re.fullmatch(r"(?:Compare|Contrast) (.+?) with (.+?)[.?]?", text, re.IGNORECASE)
    if not match or len(re.findall(r"\bwith\b", text, re.IGNORECASE)) != 1:
        return []
    return list(match.groups())


def supporting_query(question: str) -> str | None:
    """One focused topic for an already-supported answer; never an answer rewrite.

    Ambiguous qualifiers, negation and compound targets retain the original path.
    These templates identify a question part, not a claimed relationship or fact.
    """
    text = question.strip().rstrip("?.")
    if (not text or len(text) > 400 or any(char in text for char in ":;,\n?")
            or re.search(r"\b(?:and|or|not|no|never|without|unless|except|only|if|when|while|although|despite|because|but|rather|than|instead|otherwise|solely|exclusively)\b", text, re.IGNORECASE)):
        return None
    # Look for the transaction boundary only after the original question
    # has independently found evidence about the named write.
    rollback = re.fullmatch(
        r"Why (?:cannot|can['’]t) ((?:a |the )?"
        r"(?:database|SQL|PostgreSQL|MySQL|SQLite|SQL Server) rollback) "
        r"(?:remove|undo|revert) (?:partial )?[\w-]+ writes",
        text, re.IGNORECASE,
    )
    if rollback:
        return f"Can {rollback[1]} undo writes outside the transaction?"
    learning = re.fullmatch(
        r"What (?:must|should|do) I (?:understand|learn|know|study) "
        r"(?:(directly|immediately) )?before (.+)", text, re.IGNORECASE)
    if learning:
        return f"{'direct prerequisite' if learning[1] else 'prerequisites'} for {learning[2]}"
    permission = re.fullmatch(r"Why (?:does|do) (.+?) still (?:need|require) (.+)", text, re.IGNORECASE)
    if permission and not _SCOPE_MODIFIER.search(permission[1]):
        return permission[2]
    alignment = re.fullmatch(r"Why (?:must|should) (.+?) use the same (.+?) as (.+)", text, re.IGNORECASE)
    if (alignment and len(re.findall(r"\bas\b", text, re.IGNORECASE)) == 1
            and not any(_SCOPE_MODIFIER.search(part) for part in alignment.groups())):
        return alignment[1]
    return None
