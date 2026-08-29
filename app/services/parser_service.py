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
            chunk_index = 0
            for section_heading, section_text in self._split_sections(text):
                for chunk_text in self._split_text(section_text):
                    chunk_id = f"{upload_id}:{page_index}:{chunk_index}"
                    chunks.append(
                        DocumentChunk(
                            id=chunk_id,
                            text=chunk_text,
                            metadata={
                                "chunk_id": chunk_id,
                                "chunk_index": chunk_index,
                                "document_id": document_id,
                                "upload_id": upload_id,
                                "document_name": document_name,
                                "page_number": page_index,
                                "section_heading": section_heading,
                            },
                        )
                    )
                    chunk_index += 1

        return chunks

    @classmethod
    def _split_sections(cls, text: str) -> list[tuple[str, str]]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return []

        sections: list[tuple[str, str]] = []
        heading = ""
        body: list[str] = []
        for line in lines:
            if cls._looks_like_heading(line):
                if body:
                    sections.append((heading, "\n".join(body)))
                    body = []
                heading = line
                continue
            body.append(line)

        if body:
            sections.append((heading, "\n".join(body)))
        if sections:
            return sections
        return [(lines[0] if len(lines[0]) <= 120 else "", text)]

    @staticmethod
    def _looks_like_heading(line: str) -> bool:
        if not 3 <= len(line) <= 120 or len(line.split()) > 12:
            return False
        if line.endswith((".", "?", "!", ",", ";")):
            return False
        letters = [character for character in line if character.isalpha()]
        if not letters:
            return False
        return (
            line.isupper()
            or line.istitle()
            or bool(re.match(r"^(?:\d+(?:\.\d+)*|[A-Z])(?:[.)\s-])", line))
        )

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
