from dataclasses import dataclass
import re

import pymupdf as fitz


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    id: str
    text: str
    metadata: dict[str, str | int]


class ParserService:
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50) -> None:
        if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_size must be positive and larger than chunk_overlap.")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def extract_pages_from_bytes(self, content: bytes) -> list[tuple[int, str]]:
        pages: list[tuple[int, str]] = []
        with fitz.open(stream=content, filetype="pdf") as document:
            for page_index, page in enumerate(document, start=1):
                text = page.get_text("text").strip()
                if text:
                    pages.append((page_index, text))
        return pages

    def chunk_pages(
        self,
        pages: list[tuple[int, str]],
        document_id: str,
        upload_id: str,
        document_name: str = "",
    ) -> list[DocumentChunk]:
        chunks: list[DocumentChunk] = []
        for page_index, text in pages:
            section_heading = next(
                (line.strip() for line in text.splitlines() if 3 <= len(line.strip()) <= 120),
                None,
            )
            for index, chunk_text in enumerate(self._split_text(text)):
                chunk_id = f"{upload_id}:{page_index}:{index}"
                chunks.append(
                    DocumentChunk(
                        id=chunk_id,
                        text=chunk_text,
                        metadata={
                            "chunk_id": chunk_id,
                            "chunk_index": index,
                            "document_id": document_id,
                            "upload_id": upload_id,
                            "document_name": document_name,
                            "page_number": page_index,
                            "section_heading": section_heading or "",
                        },
                    )
                )

        return chunks

    def _split_text(self, text: str) -> list[str]:
        words = list(re.finditer(r"\S+", text))
        if not words:
            return []
        step = self.chunk_size - self.chunk_overlap
        chunks: list[str] = []
        for start in range(0, len(words), step):
            end = min(start + self.chunk_size, len(words))
            chunk = text[words[start].start() : words[end - 1].end()].strip()
            if chunk:
                chunks.append(chunk)
            if end == len(words):
                break
        return chunks
