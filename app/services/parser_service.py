import csv
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from io import StringIO
import math
import os
import re
import subprocess
import time
import unicodedata

import pymupdf as fitz

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    id: str
    text: str
    metadata: dict[str, str | int | float | None]


@dataclass(frozen=True, slots=True)
class LayoutLine:
    text: str
    font_size: float
    bold: bool
    bbox: tuple[float, float, float, float]
    page_height: float


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int
    text: str
    extraction_method: str = "native_text"
    ocr_confidence: float | None = None
    layout_lines: tuple[LayoutLine, ...] = ()
    body_font_size: float | None = None
    repeated_margin_lines: tuple[str, ...] = ()
    supplemental_ocr_text: str = ""
    text_quality_score: float | None = None
    extraction_warnings: tuple[str, ...] = ()
    ocr_attempts: int = 0


@dataclass(frozen=True, slots=True)
class OCRResult:
    text: str
    confidence: float | None
    attempts: int = 1
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TextQuality:
    readable_characters: int
    score: float


@dataclass(frozen=True, slots=True)
class DetectedSection:
    heading: str
    text: str
    detection_method: str
    heading_score: int | None = None


class ParserService:
    MIN_TEXT_QUALITY = 0.65
    MIN_IMAGE_AREA_RATIO = 0.10

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        *,
        ocr_enabled: bool = settings.ocr_enabled,
        ocr_language: str = settings.ocr_language,
        ocr_dpi: int = settings.ocr_dpi,
        ocr_min_native_characters: int = settings.ocr_min_native_characters,
        ocr_max_pages_per_document: int = settings.ocr_max_pages_per_document,
        ocr_page_timeout_seconds: int = settings.ocr_page_timeout_seconds,
        ocr_threads: int = settings.ocr_threads,
        ocr_min_confidence: float = settings.ocr_min_confidence,
        ocr_max_render_pixels: int = settings.ocr_max_render_pixels,
        ocr_mixed_pages_enabled: bool = settings.ocr_mixed_pages_enabled,
        ocr_retry_enabled: bool = settings.ocr_retry_enabled,
    ) -> None:
        if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_size must be positive and larger than chunk_overlap.")
        if ocr_max_render_pixels <= 0:
            raise ValueError("The OCR rendering pixel budget must be positive.")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.ocr_enabled = ocr_enabled
        self.ocr_language = ocr_language
        self.ocr_dpi = ocr_dpi
        self.ocr_min_native_characters = ocr_min_native_characters
        self.ocr_max_pages_per_document = ocr_max_pages_per_document
        self.ocr_page_timeout_seconds = ocr_page_timeout_seconds
        if not 1 <= ocr_threads <= 4:
            raise ValueError("OCR threads must be between 1 and 4.")
        self.ocr_threads = ocr_threads
        self.ocr_min_confidence = ocr_min_confidence
        self.ocr_max_render_pixels = ocr_max_render_pixels
        self.ocr_mixed_pages_enabled = ocr_mixed_pages_enabled
        self.ocr_retry_enabled = ocr_retry_enabled

    def extract_pages_from_bytes(self, content: bytes) -> list[ExtractedPage]:
        pages: list[ExtractedPage] = []
        ocr_attempts = 0
        with fitz.open(stream=content, filetype="pdf") as document:
            native_pages: list[tuple[int, str, tuple[LayoutLine, ...]]] = []
            for page_index, page in enumerate(document, start=1):
                layout = page.get_text(
                    "dict",
                    flags=fitz.TEXTFLAGS_TEXT,
                    sort=True,
                )
                layout_lines = tuple(
                    self._layout_lines_from_dict(layout, page.rect.height)
                )
                native_text = self._clean_text(
                    "\n".join(line.text for line in layout_lines)
                )
                native_pages.append((page_index, native_text, layout_lines))

            body_font_size = self._body_font_size(
                line
                for _, _, layout_lines in native_pages
                for line in layout_lines
            )
            repeated_margin_lines = tuple(
                sorted(self._repeated_margin_lines(native_pages))
            )

            for page_index, native_text, layout_lines in native_pages:
                page = document[page_index - 1]
                quality = self._text_quality(native_text)
                native_character_count = quality.readable_characters
                needs_ocr = (
                    self.ocr_enabled
                    and (native_character_count < self.ocr_min_native_characters
                         or quality.score < self.MIN_TEXT_QUALITY)
                )
                warnings: list[str] = []
                image_region = None
                if self.ocr_enabled and self.ocr_mixed_pages_enabled and not needs_ocr:
                    try:
                        image_region = self._image_ocr_region(page, layout_lines)
                    except (RuntimeError, ValueError):
                        warnings.append("image_region_unavailable")
                result: OCRResult | None = None
                supplemental = ""
                if native_text and quality.score < self.MIN_TEXT_QUALITY:
                    warnings.append("native_text_quality_low")

                if (needs_ocr or image_region is not None) and ocr_attempts >= self.ocr_max_pages_per_document:
                    if needs_ocr:
                        raise ValueError("OCR page limit exceeded. Split this scanned PDF into smaller files.")
                    warnings.append("ocr_page_budget_reached")
                    image_region = None
                if needs_ocr or image_region is not None:
                    ocr_attempts += 1
                    try:
                        result = self._ocr_page(page) if image_region is None else self._ocr_page(page, clip=image_region)
                    except (RuntimeError, ValueError) as exc:
                        if not native_text or quality.score < self.MIN_TEXT_QUALITY:
                            raise
                        # A failed image-region supplement must not discard good native text.
                        warnings.append("ocr_timeout" if "timed out" in str(exc)
                                        else "ocr_unavailable" if "unavailable" in str(exc) else "ocr_failed")
                    if result is not None:
                        warnings.extend(result.warnings)
                    if needs_ocr and result is not None and self._prefer_ocr(native_text, result):
                        pages.append(
                            ExtractedPage(
                                page_number=page_index,
                                text=result.text,
                                extraction_method="ocr",
                                ocr_confidence=result.confidence,
                                text_quality_score=self._text_quality(result.text).score,
                                extraction_warnings=tuple(dict.fromkeys(result.warnings)),
                                ocr_attempts=result.attempts,
                            )
                        )
                        continue
                    if result is not None:
                        if image_region is not None and self._usable_ocr(result):
                            supplemental = self._new_ocr_lines(native_text, result.text)
                        elif needs_ocr or result.text.strip():
                            warnings.append("ocr_did_not_improve_text")

                if native_text:
                    combined = f"{native_text}\n{supplemental}" if supplemental else native_text
                    pages.append(
                        ExtractedPage(
                            page_number=page_index,
                            text=combined,
                            extraction_method="native_text+ocr" if supplemental else "native_text",
                            ocr_confidence=result.confidence if supplemental and result else None,
                            layout_lines=layout_lines,
                            body_font_size=body_font_size,
                            repeated_margin_lines=repeated_margin_lines,
                            supplemental_ocr_text=supplemental,
                            text_quality_score=self._text_quality(combined).score,
                            extraction_warnings=tuple(dict.fromkeys(warnings)),
                            ocr_attempts=(result.attempts if result else 1) if needs_ocr or image_region is not None else 0,
                        )
                    )
                elif needs_ocr:
                    # Retain a page outcome for review, but never index rejected OCR text.
                    pages.append(ExtractedPage(
                        page_number=page_index, text="", extraction_method="unreadable",
                        extraction_warnings=tuple(dict.fromkeys([*warnings, "ocr_no_reliable_text"])),
                        ocr_attempts=result.attempts if result else 1,
                        text_quality_score=0.0,
                    ))
        return pages

    def chunk_pages(
        self,
        pages: Sequence[ExtractedPage | tuple[int, str]],
        document_id: str,
        upload_id: str,
        document_name: str = "",
        *,
        max_chunks: int | None = None,
    ) -> list[DocumentChunk]:
        if max_chunks is not None and max_chunks < 1:
            raise ValueError("max_chunks must be positive.")
        chunks: list[DocumentChunk] = []
        for page in pages:
            if isinstance(page, ExtractedPage):
                page_index = page.page_number
                text = page.text
                extraction_method = page.extraction_method
                ocr_confidence = page.ocr_confidence
                sections = (
                    self._split_layout_sections(page)
                    if page.layout_lines
                    else self._split_sections(text)
                )
                if page.supplemental_ocr_text and page.layout_lines:
                    sections.extend(self._split_sections(page.supplemental_ocr_text))
            else:
                page_index, text = page
                extraction_method = "native_text"
                ocr_confidence = None
                sections = self._split_sections(text)
            chunk_index = 0
            for section in sections:
                for chunk_text in self._iter_text_chunks(section.text):
                    if max_chunks is not None and len(chunks) >= max_chunks:
                        from app.services.resource_budget_service import ResourceBudgetExceeded
                        raise ResourceBudgetExceeded("This PDF exceeds the configured per-document chunk budget. Split it into smaller PDFs.", retryable=False)
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
                                "section_heading": section.heading,
                                "heading_detection_method": section.detection_method,
                                "heading_score": section.heading_score,
                                "extraction_method": extraction_method,
                                "ocr_confidence": ocr_confidence,
                                "text_quality_score": page.text_quality_score if isinstance(page, ExtractedPage) else None,
                                "extraction_warning": ", ".join(page.extraction_warnings) if isinstance(page, ExtractedPage) else "",
                            },
                        )
                    )
                    chunk_index += 1

        return chunks

    @staticmethod
    def _layout_lines_from_dict(
        layout: dict,
        page_height: float,
    ) -> list[LayoutLine]:
        lines: list[LayoutLine] = []
        for block in layout.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = [
                    span
                    for span in line.get("spans", [])
                    if ParserService._clean_text(str(span.get("text") or ""))
                ]
                if not spans:
                    continue
                text = ParserService._clean_text(
                    "".join(str(span.get("text") or "") for span in spans)
                )
                if not text:
                    continue
                weighted_sizes: list[tuple[float, int]] = []
                bold_characters = 0
                total_characters = 0
                for span in spans:
                    span_text = str(span.get("text") or "")
                    weight = max(
                        1,
                        ParserService._meaningful_character_count(span_text),
                    )
                    size = float(span.get("size") or 0)
                    if size > 0:
                        weighted_sizes.append((size, weight))
                    total_characters += weight
                    font_name = str(span.get("font") or "").casefold()
                    flags = int(span.get("flags") or 0)
                    if flags & fitz.TEXT_FONT_BOLD or any(
                        marker in font_name
                        for marker in ("bold", "semibold", "demi", "black")
                    ):
                        bold_characters += weight
                bbox_value = line.get("bbox") or block.get("bbox") or (0, 0, 0, 0)
                bbox = tuple(float(value) for value in bbox_value)
                if len(bbox) != 4:
                    bbox = (0.0, 0.0, 0.0, 0.0)
                lines.append(
                    LayoutLine(
                        text=text,
                        font_size=ParserService._weighted_median(weighted_sizes),
                        bold=bold_characters >= max(1, math.ceil(total_characters / 2)),
                        bbox=bbox,
                        page_height=float(page_height),
                    )
                )
        return lines

    @staticmethod
    def _weighted_median(values: Sequence[tuple[float, int]]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values, key=lambda item: item[0])
        midpoint = sum(weight for _, weight in ordered) / 2
        cumulative = 0
        for value, weight in ordered:
            cumulative += weight
            if cumulative >= midpoint:
                return value
        return ordered[-1][0]

    @staticmethod
    def _body_font_size(lines: Iterable[LayoutLine]) -> float:
        weighted_sizes: list[tuple[float, int]] = []
        for line in lines:
            if line.font_size <= 0:
                continue
            weight = max(1, ParserService._meaningful_character_count(line.text))
            weighted_sizes.append((line.font_size, weight))
        return ParserService._weighted_median(weighted_sizes) or 11.0

    @staticmethod
    def _normalized_margin_text(value: str) -> str:
        normalized = " ".join(value.casefold().split())
        return re.sub(r"\d+", "#", normalized)

    @classmethod
    def _repeated_margin_lines(
        cls,
        pages: Sequence[tuple[int, str, tuple[LayoutLine, ...]]],
    ) -> set[str]:
        occurrences: Counter[str] = Counter()
        for _, _, lines in pages:
            seen_on_page: set[str] = set()
            for line in lines:
                in_margin = (
                    line.bbox[1] <= line.page_height * 0.12
                    or line.bbox[3] >= line.page_height * 0.88
                )
                normalized = cls._normalized_margin_text(line.text)
                if in_margin and normalized:
                    seen_on_page.add(normalized)
            occurrences.update(seen_on_page)
        required_pages = max(2, math.ceil(len(pages) * 0.3))
        return {
            text
            for text, count in occurrences.items()
            if count >= required_pages
        }

    @classmethod
    def _split_layout_sections(cls, page: ExtractedPage) -> list[DetectedSection]:
        repeated = set(page.repeated_margin_lines)
        visible_lines = [
            line
            for line in page.layout_lines
            if cls._normalized_margin_text(line.text) not in repeated
        ]
        if not visible_lines:
            return []

        sections: list[DetectedSection] = []
        heading = ""
        heading_score: int | None = None
        body: list[str] = []
        body_font_size = page.body_font_size or cls._body_font_size(visible_lines)
        for index, line in enumerate(visible_lines):
            previous = visible_lines[index - 1] if index > 0 else None
            following = visible_lines[index + 1] if index + 1 < len(visible_lines) else None
            score = cls._layout_heading_score(
                line,
                body_font_size=body_font_size,
                previous=previous,
                following=following,
            )
            if score >= 4:
                if body:
                    sections.append(
                        DetectedSection(
                            heading=heading,
                            text="\n".join(body),
                            detection_method="layout",
                            heading_score=heading_score,
                        )
                    )
                    body = []
                heading = line.text
                heading_score = score
                continue
            body.append(line.text)

        if body:
            sections.append(
                DetectedSection(
                    heading=heading,
                    text="\n".join(body),
                    detection_method="layout",
                    heading_score=heading_score,
                )
            )
        return sections or [
            DetectedSection(
                heading="",
                text="\n".join(line.text for line in visible_lines),
                detection_method="layout",
            )
        ]

    @classmethod
    def _layout_heading_score(
        cls,
        line: LayoutLine,
        *,
        body_font_size: float,
        previous: LayoutLine | None,
        following: LayoutLine | None,
    ) -> int:
        text = line.text.strip()
        word_count = len(text.split())
        if len(text) < 3 or not any(character.isalpha() for character in text):
            return -10

        score = 0
        font_ratio = line.font_size / max(body_font_size, 1)
        if font_ratio >= 1.45:
            score += 2
        elif font_ratio >= 1.2:
            score += 1
        if line.bold:
            score += 2
        if word_count <= 12 and len(text) <= 120:
            score += 1
        numbered = bool(
            re.match(r"^(?:\d+(?:\.\d+)*|[A-Z])(?:[.)\s-])", text)
        )
        if numbered:
            score += 1
        if text.isupper() or text.istitle():
            score += 1

        gap_threshold = max(4.0, body_font_size * 0.45)
        gap_above = (
            max(0.0, line.bbox[1] - previous.bbox[3])
            if previous is not None
            else 0.0
        )
        gap_below = (
            max(0.0, following.bbox[1] - line.bbox[3])
            if following is not None
            else 0.0
        )
        if gap_above >= gap_threshold or gap_below >= gap_threshold:
            score += 1
        if text.endswith((".", "?", "!", ",", ";")):
            score -= 2
        if len(text) > 120 or word_count > 16:
            score -= 2
        return score

    @classmethod
    def _text_quality(cls, text: str) -> TextQuality:
        """A deterministic corruption signal, not a probability of factual accuracy."""
        characters = [char for char in text if not char.isspace()]
        readable = cls._meaningful_character_count(text)
        if not characters or readable == 0:
            return TextQuality(readable, 0.0)
        bad = sum(char == "\ufffd" or unicodedata.category(char) in {"Cc", "Cs", "Cn"} for char in characters)
        repeated = sum(len(match.group()) for match in re.finditer(r"([^\W\d_])\1{5,}", text))
        symbols = sum(not char.isalnum() for char in characters) / len(characters)
        score = 1 - 4 * bad / len(characters) - 2 * repeated / len(characters) - max(0, symbols - 0.5)
        return TextQuality(readable, round(max(0.0, min(1.0, score)), 3))

    def _usable_ocr(self, result: OCRResult) -> bool:
        quality = self._text_quality(result.text)
        return (quality.readable_characters >= 10 and quality.score >= self.MIN_TEXT_QUALITY
                and (result.confidence is None or (math.isfinite(result.confidence)
                     and result.confidence >= self.ocr_min_confidence)))

    def _prefer_ocr(self, native_text: str, result: OCRResult) -> bool:
        if not self._usable_ocr(result):
            return False
        native = self._text_quality(native_text)
        recovered = self._text_quality(result.text)
        return (recovered.score > native.score + 0.15
                or (native.readable_characters < self.ocr_min_native_characters
                    and recovered.readable_characters > native.readable_characters
                    and recovered.score >= native.score))

    @staticmethod
    def _new_ocr_lines(native_text: str, ocr_text: str) -> str:
        def normalize(text: str) -> str:
            return " ".join(re.findall(r"\w+", text.casefold()))
        native = f" {normalize(native_text)} "
        retained: list[str] = []
        seen: set[str] = set()
        for line in ocr_text.splitlines():
            key = normalize(line)
            if not key or key in seen or f" {key} " in native:
                continue
            seen.add(key)
            retained.append(line.strip())
        return "\n".join(retained)

    def _image_ocr_region(self, page: fitz.Page, lines: Sequence[LayoutLine]) -> fitz.Rect | None:
        regions: list[fitz.Rect] = []
        page_area = page.rect.get_area()
        for image in page.get_image_info():
            rect = (fitz.Rect(image["bbox"]) * page.rotation_matrix) & page.rect
            area = rect.get_area()
            if area < page_area * self.MIN_IMAGE_AREA_RATIO:
                continue
            covered_area = 0.0
            covered_characters = 0
            for line in lines:
                intersection = (fitz.Rect(line.bbox) * page.rotation_matrix) & rect
                if intersection.is_empty:
                    continue
                covered_area += intersection.get_area()
                covered_characters += self._meaningful_character_count(line.text)
            if covered_characters >= self.ocr_min_native_characters and covered_area / area >= 0.12:
                continue
            regions.append(rect)
        if not regions:
            return None
        combined = fitz.Rect(regions[0])
        for rect in regions[1:]:
            combined |= rect
        return combined

    def _render_ocr_page(self, page: fitz.Page, *, dpi: int, clip: fitz.Rect | None = None) -> tuple[fitz.Pixmap, int]:
        rect = page.rect if clip is None else clip & page.rect
        if rect.is_empty:
            raise RuntimeError("The OCR engine could not process an empty page region.")
        scale = min(dpi / 72, 0.98 * math.sqrt(self.ocr_max_render_pixels / rect.get_area()),
                    6000 / max(rect.width, rect.height))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, colorspace=fitz.csGRAY, alpha=False)
        return pixmap, max(1, round(scale * 72))

    def _ocr_page(self, page: fitz.Page, *, clip: fitz.Rect | None = None) -> OCRResult:
        deadline = time.monotonic() + self.ocr_page_timeout_seconds
        pixmap, dpi = self._render_ocr_page(page, dpi=self.ocr_dpi, clip=clip)
        result = self._run_tesseract(pixmap, dpi=dpi, psm=1, timeout=max(0.001, deadline - time.monotonic()))
        attempts = 1
        warnings: list[str] = ["ocr_resolution_capped"] if dpi < self.ocr_dpi else []
        if not self._usable_ocr(result) and self.ocr_retry_enabled and time.monotonic() < deadline:
            # One alternative segmentation at bounded resolution; both calls share a deadline.
            retry_image, retry_dpi = self._render_ocr_page(page, dpi=min(300, self.ocr_dpi + 100), clip=clip)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                warnings.append("ocr_retry_timeout")
            else:
                try:
                    attempts += 1
                    alternate = self._run_tesseract(retry_image, dpi=retry_dpi, psm=6, timeout=remaining)
                    if self._ocr_rank(alternate) > self._ocr_rank(result):
                        result = alternate
                except RuntimeError:
                    warnings.append("ocr_retry_failed")
        if result.confidence is not None and result.confidence < self.ocr_min_confidence:
            warnings.append("ocr_confidence_low")
        if self._text_quality(result.text).score < self.MIN_TEXT_QUALITY:
            warnings.append("ocr_text_quality_low")
        return replace(result, attempts=attempts, warnings=tuple(dict.fromkeys(warnings)))

    def _ocr_rank(self, result: OCRResult) -> tuple[bool, float, float, int]:
        quality = self._text_quality(result.text)
        return (self._usable_ocr(result), quality.score, result.confidence or 0.0, quality.readable_characters)

    def _run_tesseract(self, pixmap: fitz.Pixmap, *, dpi: int, psm: int, timeout: float) -> OCRResult:
        command = [
            "tesseract",
            "stdin",
            "stdout",
            "-l",
            self.ocr_language,
            "--dpi",
            str(dpi),
            "--psm",
            str(psm),
            "tsv",
        ]
        try:
            completed = subprocess.run(
                command,
                input=pixmap.tobytes("png"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
                env={**os.environ, "OMP_THREAD_LIMIT": str(self.ocr_threads)},
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "The OCR engine is unavailable. Install Tesseract or disable OCR."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("The OCR engine timed out while processing a page.") from exc

        if completed.returncode != 0:
            raise RuntimeError("The OCR engine could not process a PDF page.")
        return self._parse_tesseract_tsv(
            completed.stdout.decode("utf-8", errors="replace")
        )

    @staticmethod
    def _parse_tesseract_tsv(value: str) -> OCRResult:
        lines: dict[tuple[str, str, str], list[str]] = {}
        confidences: list[tuple[float, int]] = []
        for row in csv.DictReader(StringIO(value), delimiter="\t"):
            word = ParserService._clean_text(row.get("text") or "")
            if not word:
                continue
            try:
                confidence = float(row.get("conf", "-1"))
            except (TypeError, ValueError):
                confidence = -1
            if not math.isfinite(confidence) or not 0 <= confidence <= 100:
                continue
            line_key = (
                row.get("block_num", ""),
                row.get("par_num", ""),
                row.get("line_num", ""),
            )
            lines.setdefault(line_key, []).append(word)
            weight = max(1, ParserService._meaningful_character_count(word))
            confidences.append((confidence, weight))

        text = "\n".join(" ".join(words) for words in lines.values()).strip()
        total_weight = sum(weight for _, weight in confidences)
        average = (
            round(
                sum(confidence * weight for confidence, weight in confidences)
                / total_weight,
                1,
            )
            if total_weight
            else None
        )
        return OCRResult(text=text, confidence=average)

    @staticmethod
    def _clean_text(value: str) -> str:
        return value.replace("\x00", "").strip()

    @staticmethod
    def _meaningful_character_count(value: str) -> int:
        return sum(character.isalnum() for character in value)

    @classmethod
    def _split_sections(cls, text: str) -> list[DetectedSection]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return []

        sections: list[DetectedSection] = []
        heading = ""
        body: list[str] = []
        for line in lines:
            if cls._looks_like_heading(line):
                if body:
                    sections.append(
                        DetectedSection(
                            heading=heading,
                            text="\n".join(body),
                            detection_method="text_heuristic",
                        )
                    )
                    body = []
                heading = line
                continue
            body.append(line)

        if body:
            sections.append(
                DetectedSection(
                    heading=heading,
                    text="\n".join(body),
                    detection_method="text_heuristic",
                )
            )
        if sections:
            return sections
        return [
            DetectedSection(
                heading=lines[0] if len(lines[0]) <= 120 else "",
                text=text,
                detection_method="text_heuristic",
            )
        ]

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
        return list(self._iter_text_chunks(text))

    def _iter_text_chunks(self, text: str):
        words = deque()
        new_words = 0
        for match in re.finditer(r"\S+", text):
            words.append(match)
            new_words += 1
            if len(words) == self.chunk_size:
                yield text[words[0].start():words[-1].end()].strip()
                for _ in range(self.chunk_size - self.chunk_overlap):
                    words.popleft()
                new_words = 0
        if words and new_words:
            yield text[words[0].start():words[-1].end()].strip()
