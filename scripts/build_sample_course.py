"""Build the authored PDF fixture and static public demonstration (no services)."""
from pathlib import Path
import json
import sys
import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]


def build():
    course = json.loads((ROOT / "evaluation/course.json").read_text())
    output = ROOT / "public/sample"
    output.mkdir(parents=True, exist_ok=True)
    for doc in course["documents"]:
        pdf = fitz.open()
        for number, content in enumerate(doc["pages"], 1):
            page = pdf.new_page(width=595, height=842)
            page.insert_text((48, 48), "CONCEPTGRAPH / COMPUTING FOUNDATIONS", fontsize=10, color=(0.05, 0.45, 0.42))
            page.insert_text((48, 105), content["title"], fontsize=23)
            remaining = page.insert_textbox(fitz.Rect(48, 140, 547, 700), content["text"], fontsize=13, lineheight=1.6)
            if remaining < 0:
                raise ValueError(f"Page overflow: {doc['filename']} page {number}")
            page.insert_text((48, 775), f"Original educational fixture | {doc['filename']} | Page {number}", fontsize=9)
        pdf.save(output / doc["filename"])
        pdf.close()
    # Static, explicitly editorial examples are available even if Render is asleep.
    (output / "course.json").write_text(json.dumps(course, indent=2) + "\n")
    print(f"Built {len(course['documents'])} PDFs and saved examples in {output}")


if __name__ == "__main__":
    build()
