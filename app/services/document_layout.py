"""Reading order for native PDF pages. Coordinates use unrotated PDF points."""

from dataclasses import replace
from statistics import median

import pymupdf as fitz


def ordered_lines(page, lines):
    if not lines:
        return ()
    width = page.cropbox.width
    # Use vector rules to distinguish table rows from aligned prose columns.
    # Bound the work on drawings with large numbers of vector paths.
    paths = page.get_drawings()
    if paths and len(paths) <= 1000 and sum(len(path["items"]) for path in paths) <= 4000:
        try:
            tables = page.find_tables(paths=paths, strategy="lines_strict").tables
        except (RuntimeError, ValueError):
            tables = []
        for table in tables:
            rect = fitz.Rect(table.bbox)
            inside = [line for line in lines if rect.contains(
                fitz.Point((line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2)
            )]
            if not inside:
                continue
            table_rows = []
            for row, cells in zip(table.rows, table.extract()):
                text = " | ".join(" ".join((cell or "").split()) for cell in cells)
                if any(cell and cell.strip() for cell in cells):
                    table_rows.append(replace(inside[0], text=text, bbox=tuple(row.bbox), kind="table_row"))
            if table_rows:
                consumed = set(inside)
                lines = [line for line in lines if line not in consumed] + table_rows

    ordered = sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))
    # A heading, table or other line spanning the central gutter divides
    # the page into independent bands. Within each band read columns top down.
    result, band = [], []
    def flush():
        left = [line for line in band if line.bbox[2] <= width / 2]
        right = [line for line in band if line.bbox[0] >= width / 2]
        if len(left) >= 2 and len(right) >= 2:
            gap = min(line.bbox[0] for line in right) - max(line.bbox[2] for line in left)
            overlap = min(max(l.bbox[3] for l in col) for col in (left, right)) - max(min(l.bbox[1] for l in col) for col in (left, right))
            if gap >= width * 0.035 and overlap > median(l.bbox[3] - l.bbox[1] for l in band):
                result.extend(left + right)
                band.clear()
                return
        result.extend(band)
        band.clear()
    for line in ordered:
        if line.kind == "table_row" or line.bbox[0] < width / 2 < line.bbox[2]:
            flush()
            result.append(line)
        else:
            band.append(line)
    flush()
    return tuple(result)
