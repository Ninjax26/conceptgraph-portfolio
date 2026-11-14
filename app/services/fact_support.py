"""Conservative explicit equipment facts, not general semantic entailment.

Only literal cited text can establish manufacturer, voltage or serial facts for
named identifiers. Other question forms keep the existing evidence policy.
"""
from dataclasses import dataclass
import re

from app.services.evidence_facets import comparison_facets
from app.services.retrieval_candidates import DASHES, IDENTIFIER, identifier_supported, identifier_terms


@dataclass(frozen=True)
class FactRequest:
    attribute: str
    identifier: str
    qualifier: str | None = None


_LOOKUP = re.compile(r"\b(?:what|which|who|find|give|state|list|tell me)\b", re.I)
_DEFINITION = re.compile(r"\b(?:mean|means|meaning|define|definition|explain|why|learn|study|understand|prerequisite)\b", re.I)
_UNCERTAIN = re.compile(r"\b(?:not|no|unknown|unspecified|unavailable|unlisted|unrecorded|undocumented|missing|tbd|none|n/a|may|maybe|might|could|would|should|if|unless|assuming|suppose|example|hypothetical|never|unconfirmed|possibly|proposed|planned|rather|instead|uncertain|unverified|incorrect|invalid|wrong)\b", re.I)
_ILLUSTRATIVE = re.compile(r"\b(?:example|hypothetical|suppose|assuming|illustration)\b", re.I)
_FOREIGN_SUBJECT = re.compile(
    r"\b(?:other|another|different)\s+(?:device|equipment|unit|model)\b|"
    r"\b(?:adapter|charger|cable|label|package|packaging|accessory|component|sensor|controller|motor)"
    r"\s+(?:for|of|(?:attached|connected)\s+to)\b", re.I)
_UNKNOWN_VALUE = re.compile(
    r"^(?:see|refer|consult|check|contact)\b|^to\s+be\b|"
    r"^(?:n/?a|n\.a\.?|tba|tbd|pending|unassigned|unavailable|unspecified|none|null|nil)$", re.I)
_ARTIFACT = re.compile(r"\b(?:manual|document|brochure|report|illustration|diagram)\b", re.I)
_SENTENCE = re.compile(r"\S.*?(?:[.!?](?=\s|$)|;|$)")
_HEADER = re.compile(r"\bEquipment(?:\s+identifier)?\s*[:=]?\s*([A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*)\b", re.I)
_MANUFACTURER = re.compile(
    r"\b(?:(?:manufacturer|maker)\s*(?::|=|is)\s*|(?:manufactured|made|produced)\s+by\s+)"
    r"(?P<value>[^.;\n]{1,100})", re.I)
_MANUFACTURER_ACTIVE = re.compile(
    r"\b(?P<value>(?-i:[A-Z][A-Za-z0-9&'-]*(?:\s+(?:[A-Z][A-Za-z0-9&'-]*|&)){0,7}))\s+(?:manufactures|makes|produces)\s+"
    r"(?P<subject>[A-Za-z][A-Za-z0-9_.-]*[0-9][A-Za-z0-9_.-]*)\b", re.I)
_VOLTAGE = re.compile(
    r"\b(?P<label>(?:(?:input|output|supply|rated|operating|nominal|maximum|electrical)\s+){0,3}voltage)"
    r"\s*(?:is\b|of\b|[:=])?\s*(?P<value>\d+(?:\.\d+)?(?:\s*(?:-|to|±)\s*\d+(?:\.\d+)?)?"
    r"\s*(?:kV|mV|V|volts?)(?:\s*(?:AC|DC))?)\b", re.I)
_SERIAL = re.compile(r"\bserial\s*(?:number\b|no\b\.?|#)\s*(?::|=|is\b)?\s*(?P<value>[A-Za-z0-9][A-Za-z0-9_.-]*)", re.I)
_PATTERNS = {'manufacturer': (_MANUFACTURER, _MANUFACTURER_ACTIVE), 'voltage': (_VOLTAGE,), 'serial': (_SERIAL,)}


def _attributes(part: str) -> list[tuple[str, str | None]]:
    result: list[tuple[str, str | None]] = []
    if re.search(r"\b(?:manufacturer|maker|manufactured|manufactures|makes|made|produced)\b", part, re.I):
        result.append(('manufacturer', None))
    if re.search(r"\b(?:voltage|volts)\b", part, re.I):
        qualifiers = re.findall(r"\b(?:input|supply|output|nominal|rated|maximum)\b", part, re.I)
        result.extend(('voltage', 'input' if word.lower() == 'supply' else word.lower())
                      for word in qualifiers)
        if not qualifiers:
            result.append(('voltage', None))
    if re.search(r"\bserial\s*(?:number|no\.?|#)\b", part, re.I):
        result.append(('serial', None))
    return list(dict.fromkeys(result))


def requested_facts(question: str, *, fragment: bool = False) -> tuple[FactRequest, ...]:
    if _DEFINITION.search(question) or (not fragment and not _LOOKUP.search(question) and not comparison_facets(question)):
        return ()
    parts = comparison_facets(question)
    if not parts:
        identifiers = identifier_terms(question, limit=9, named_only=True)
        attributes = _attributes(question)
        if not identifiers or not attributes:
            return ()
        clauses = re.split(r"\band\b", question, flags=re.I)
        independent = len(clauses) > 1 and all(identifier_terms(part, named_only=True) for part in clauses)
        if independent:
            # A tail consisting only of identifiers is coordination (A and B),
            # not an independently requested attribute with a different subject.
            tail_words = [set(re.findall(r"[a-z]+", IDENTIFIER.sub('', part).lower())) for part in clauses[1:]]
            independent = any(words - {'the', 'also'} for words in tail_words)
        if independent:
            return tuple(dict.fromkeys(FactRequest(attribute, identifier, qualifier)
                                       for part in clauses for attribute, qualifier in _attributes(part)
                                       for identifier in identifier_terms(part, limit=9, named_only=True)))
        if len(identifiers) > 1 and len(attributes) > 1:
            # Bind independent clauses only when each names its own subject;
            # never guess which attribute belongs to which of several devices.
            parts = re.split(r"\band\b", question, flags=re.I)
            if not all(identifier_terms(part, named_only=True) and _attributes(part) for part in parts):
                return ()
        else:
            return tuple(FactRequest(attribute, identifier, qualifier)
                         for attribute, qualifier in attributes for identifier in identifiers)
    return tuple(dict.fromkeys(FactRequest(attribute, identifier, qualifier)
                               for part in parts for attribute, qualifier in _attributes(part)
                               for identifier in identifier_terms(part, limit=9, named_only=True)))


def _named_positions(text: str, excluded: list[tuple[int, int]]) -> list[tuple[str, int]]:
    result = []
    for match in IDENTIFIER.finditer(text):
        if any(start <= match.start() < end for start, end in excluded):
            continue
        terms = identifier_terms(match.group(), named_only=True)
        if terms:
            result.append((terms[0], match.start()))
    return result


def _belongs_to(request: FactRequest, text: str, match: re.Match, values: list[tuple[int, int]]) -> bool:
    sentence = next((sentence for sentence in _SENTENCE.finditer(text)
                     if sentence.start() <= match.start() < sentence.end()), None)
    if sentence is None or sentence.group().rstrip().endswith('?'):
        return False
    prefix = text[sentence.start():match.start()]
    if (_UNCERTAIN.search(prefix) or _ARTIFACT.search(prefix)
            or _ILLUSTRATIVE.search(text[:match.start()])
            or _UNCERTAIN.search(sentence.group()) or _FOREIGN_SUBJECT.search(sentence.group())):
        return False
    positions = _named_positions(text, values)
    before = [(term, position) for term, position in positions if sentence.start() <= position < match.start()]
    if before:
        return before[-1][0] == request.identifier
    in_sentence = {term for term, position in positions if sentence.start() <= position < sentence.end()}
    if in_sentence:
        return in_sentence == {request.identifier}
    headers = list(_HEADER.finditer(text[:match.start()]))
    if headers:
        terms = identifier_terms(headers[-1][1], named_only=True)
        # An explicit header binds later fields until a new header. Other named
        # references since that header make inheritance ambiguous.
        intervening = {term for term, position in positions if headers[-1].end() <= position < match.start()}
        return terms == [request.identifier] and intervening <= {request.identifier}
    # A future mention cannot assign an earlier unscoped value to this device.
    return (any(position < match.start() for _, position in positions)
            and {term for term, _ in positions} == {request.identifier})


def supports_fact(request: FactRequest, passage: str) -> bool:
    text = ' '.join(passage.split()).translate(DASHES)
    if not identifier_supported(request.identifier, text):
        return False
    matches = {attribute: [match for pattern in patterns for match in pattern.finditer(text)]
               for attribute, patterns in _PATTERNS.items()}
    values = []
    for attribute, group in matches.items():
        for match in group:
            start, end = match.span('value')
            if attribute == 'manufacturer':
                # A trailing "for EQ-..." names the subject, not part of a
                # manufacturer name. Keep that identifier available for binding.
                suffix = re.search(r"\b(?:for|of|about|equipment|device|model)\b", match['value'], re.I)
                if suffix:
                    end = start + suffix.start()
            values.append((start, end))
    for match in matches.get(request.attribute, []):
        value = match['value'].strip()
        if _UNCERTAIN.search(value) or _UNKNOWN_VALUE.search(value.rstrip('.')):
            continue
        if request.attribute == 'manufacturer':
            if re.match(r'(?:a|an|some|any|the)\s+(?:qualified|approved|registered|company|manufacturer|maker|supplier|vendor)\b', value, re.I):
                continue
            words = set(re.findall(r'[a-z]+', value.lower()))
            if not words - {'a', 'an', 'the', 'manufacturer', 'maker', 'company', 'supplier', 'vendor', 'that', 'which', 'who', 'whoever', 'this', 'these', 'some', 'any'}:
                continue
        if request.attribute == 'serial' and not (any(char.isdigit() for char in value) or value.isupper() and len(value) > 1):
            continue
        if request.attribute == 'voltage' and request.qualifier:
            labels = set(match['label'].lower().split())
            if request.qualifier == 'input':
                if not labels & {'input', 'supply'} or 'output' in labels:
                    continue
            elif request.qualifier not in labels:
                continue
        if _belongs_to(request, text, match, values):
            return True
    return False
