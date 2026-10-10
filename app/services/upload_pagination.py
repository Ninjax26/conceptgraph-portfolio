"""Opaque keyset cursors and SQL-side canonical document selection."""

import base64
from datetime import datetime
import json

from sqlalchemy import case, func, select, tuple_

from app.models.document_upload import DocumentUpload


def encode_upload_cursor(record):
    value = json.dumps([record.created_at.isoformat(), record.upload_id]).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode_upload_cursor(cursor):
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
            raise ValueError
        date, identifier = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        stamp = datetime.fromisoformat(date)
        if stamp.utcoffset() is None or not isinstance(identifier, str) or not 1 <= len(identifier) <= 64:
            raise ValueError
        return stamp, identifier
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Invalid document cursor.") from exc


async def paginated_uploads(session, *, limit=25, cursor=None):
    rank = case((DocumentUpload.status == "active", 0), (DocumentUpload.status == "ready", 1),
                (DocumentUpload.status == "failed", 2), (DocumentUpload.status == "cancelled", 3), else_=9)
    canonical = select(DocumentUpload.upload_id.label("id"), func.row_number().over(
        partition_by=(func.coalesce(DocumentUpload.course_uuid, DocumentUpload.course_id),
                      func.coalesce(DocumentUpload.content_hash, DocumentUpload.upload_id)),
        order_by=(rank, DocumentUpload.created_at.desc(), DocumentUpload.upload_id.desc()),
    ).label("position")).subquery()
    query = select(DocumentUpload).join(canonical, canonical.c.id == DocumentUpload.upload_id).where(canonical.c.position == 1)
    if cursor:
        query = query.where(tuple_(DocumentUpload.created_at, DocumentUpload.upload_id) < decode_upload_cursor(cursor))
    query = query.order_by(DocumentUpload.created_at.desc(), DocumentUpload.upload_id.desc()).limit(limit + 1)
    rows = list((await session.execute(query)).scalars().all())
    return rows[:limit], encode_upload_cursor(rows[limit - 1]) if len(rows) > limit else None
