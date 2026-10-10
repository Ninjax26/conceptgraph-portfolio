"""Fill unknown legacy page/byte budgets from retained PDFs; no AI/index writes.

Dry-run by default. --apply writes only metadata in a local PostgreSQL database.
Source object reads use the existing configured storage; content is never printed.
"""

import argparse
import asyncio
from pathlib import Path
import sys

import pymupdf as fitz
from sqlalchemy import or_, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def refresh(*, apply=False, limit=100):
    from app.core.config import settings
    from app.core.database import AsyncSessionLocal, close_database_connections
    from app.models.document_upload import DocumentUpload
    from app.services.resource_budget_service import resource_budget_service
    from app.services.storage_service import storage_service
    from scripts.evaluate_ingestion_scaling import require_local_database
    require_local_database(settings.postgres_dsn)
    result = {"inspected": 0, "updated": 0, "skipped_or_unavailable": 0, "dry_run": not apply}
    try:
        async with AsyncSessionLocal() as session:
            records = list((await session.execute(select(DocumentUpload).where(
                or_(DocumentUpload.page_count.is_(None), DocumentUpload.source_bytes.is_(None)),
                DocumentUpload.status.in_(["ready", "failed"]),
            ).order_by(DocumentUpload.upload_id).limit(limit))).scalars().all())
        for record in records:
            try:
                content = await asyncio.to_thread(storage_service.get_bytes, record.storage_key)
                with fitz.open(stream=content, filetype="pdf") as pdf:
                    if pdf.needs_pass or pdf.page_count < 1:
                        raise ValueError("Retained PDF cannot be inspected.")
                    pages = pdf.page_count
                result["inspected"] += 1
                if not apply:
                    continue
                async with AsyncSessionLocal() as session:
                    await resource_budget_service.lock(session)
                    current = (await session.execute(select(DocumentUpload).where(
                        DocumentUpload.upload_id == record.upload_id).with_for_update())).scalar_one_or_none()
                    if current is None or current.status not in {"ready", "failed"} or current.storage_key != record.storage_key:
                        result["skipped_or_unavailable"] += 1
                        continue
                    current.page_count = pages
                    current.source_bytes = len(content)
                    if current.reserved_chunk_count is None:
                        current.reserved_chunk_count = current.processed_chunk_count or resource_budget_service.chunk_reservation(pages)
                    await session.commit()
                    result["updated"] += 1
            except Exception:
                result["skipped_or_unavailable"] += 1
    finally:
        await close_database_connections()
    return result


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("Use a bounded limit between 1 and 1000.")
    print(json.dumps(asyncio.run(refresh(apply=args.apply, limit=args.limit))))
