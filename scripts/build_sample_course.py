"""Build the authored PDF fixture and static public demonstration (no services)."""
from pathlib import Path
import argparse
import json
import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]


def build_course(source: Path, output: Path, manifest: str = "course.json"):
    course = json.loads(source.read_text())
    output.mkdir(parents=True, exist_ok=True)
    for doc in course["documents"]:
        with fitz.open() as pdf:
            for number, content in enumerate(doc["pages"], 1):
                page = pdf.new_page(width=595, height=842)
                heading = course.get("title", "Computing Foundations").upper()
                page.insert_text((48, 48), f"CONCEPTGRAPH / {heading}", fontsize=10, color=(0.05, 0.45, 0.42))
                page.insert_text((48, 105), content["title"], fontsize=23)
                remaining = page.insert_textbox(fitz.Rect(48, 140, 547, 700), content["text"], fontsize=13, lineheight=1.6)
                if remaining < 0:
                    raise ValueError(f"Page overflow: {doc['filename']} page {number}")
                if doc.get("revision"):
                    page.insert_text((48, 750), doc["revision"], fontsize=9)
                page.insert_text((48, 775), f"Original educational fixture | {doc['filename']} | Page {number}", fontsize=9)
                if content.get("extraction_method") == "ocr":
                    # Rasterize the entire page: no hidden native text layer remains.
                    image = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
                    for xref in page.get_contents():
                        pdf.update_stream(xref, b"")
                    page.insert_image(page.rect, stream=image)
            pdf.save(output / doc["filename"], garbage=4, deflate=True)
    # Static, explicitly editorial examples are available even if Render is asleep.
    (output / manifest).write_text(json.dumps(course, indent=2) + "\n")
    print(f"Built {len(course['documents'])} PDFs and saved examples in {output}")


def build(technical_only: bool = False):
    output = ROOT / "public/sample"
    if not technical_only:
        build_course(ROOT / "evaluation/course.json", output)
    build_course(ROOT / "evaluation/technical-course.json", output, "course-technical.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--technical-only", action="store_true", help="Keep existing Computing PDFs unchanged")
    build(parser.parse_args().technical_only)
