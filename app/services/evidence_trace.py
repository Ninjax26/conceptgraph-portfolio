"""Opt-in offline evidence decisions. Never attached to an HTTP response or log.

Passage offsets refer to whitespace-normalized chunk text. Hashes link stages
without copying request text, source contents, filenames or document IDs.
"""
import hashlib
import math
from typing import Any


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def chunk_key(chunk: dict[str, Any]) -> str:
    metadata = chunk.get("metadata") or {}
    return fingerprint(f"{metadata.get('upload_id', '')}\0{metadata.get('page_number')}\0{chunk.get('id', '')}")


class EvidenceTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, stage: str, reason: str, chunk: dict[str, Any] | None = None,
               *, passage: str | None = None, **values: Any) -> None:
        event: dict[str, Any] = {"stage": stage, "reason": reason}
        if chunk is not None:
            metadata = chunk.get("metadata") or {}
            document = str(metadata.get("upload_id", ""))
            page = metadata.get("page_number")
            event.update(chunk_key=chunk_key(chunk),
                         document_name_key=fingerprint(str(metadata.get("document_name", ""))),
                         document_key=fingerprint(document), page=page,
                         facet=chunk.get("evidence_facet"),
                         role="supporting" if chunk.get("supporting_evidence") is True else "primary")
            if passage is not None:
                text = " ".join(str(chunk.get("text", "")).split())
                start = text.find(passage)
                event.update(passage_sha256=fingerprint(passage), characters=len(passage),
                             start=start if start >= 0 else None,
                             end=start + len(passage) if start >= 0 else None)
        event.update({key: None if isinstance(value, float) and not math.isfinite(value) else value
                      for key, value in values.items()})
        self.events.append(event)

    def finish(self, sources: list[dict[str, Any]], confidence: dict[str, Any], reason: str):
        for source in sources:
            self.record("final_source", "selected", document_key=fingerprint(source["document_id"]),
                        page=source["page_number"], passage_sha256=fingerprint(source["supporting_passage"]))
        self.record("final", reason, source_count=len(sources), evidence_level=confidence["level"])
        return sources, confidence
