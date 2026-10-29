"""Owned PostgreSQL probe for repeated startup beside an existing reader."""
import asyncio
from unittest.mock import patch

from sqlalchemy import select, text

from app.core import database
from app.models.document_upload import DocumentUpload, GraphPublicationIntent


async def verify_current_schema_startup(engine, sessions):
    initializer = joined = None
    async with sessions() as reader:
        # An ordinary recovery SELECT retains this relation lock to transaction end.
        await reader.execute(select(GraphPublicationIntent.id).limit(1))
        try:
            with patch.object(database, "postgres_engine", engine):
                initializer = asyncio.create_task(database.initialize_database_schema())
                async with asyncio.timeout(8):
                    while not initializer.done():
                        async with engine.connect() as observer:
                            exclusive = (await observer.execute(text(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE relation = "
                                "'document_uploads'::regclass AND mode = 'AccessExclusiveLock' AND granted)"
                            ))).scalar_one()
                        if exclusive:
                            joined = asyncio.create_task(reader.execute(select(GraphPublicationIntent.id).outerjoin(
                                DocumentUpload, DocumentUpload.upload_id == GraphPublicationIntent.upload_id).limit(1)))
                            outcomes = await asyncio.gather(initializer, joined, return_exceptions=True)
                            errors = [o for o in outcomes if isinstance(o, BaseException)]
                            if errors:
                                states = [getattr(getattr(e, "orig", None), "sqlstate", type(e).__name__) for e in errors]
                                raise AssertionError("Repeated startup deadlocked an existing reader: "+repr(states))
                            raise AssertionError("Repeated current-schema startup took unnecessary exclusive table locks")
                        await asyncio.sleep(.01)
                    await initializer
                    await reader.execute(select(GraphPublicationIntent.id).outerjoin(
                        DocumentUpload, DocumentUpload.upload_id == GraphPublicationIntent.upload_id).limit(1))
        finally:
            for task in (initializer, joined):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (initializer, joined) if t is not None), return_exceptions=True)
            await reader.rollback()
    return {"current_schema_restart_completes_while_existing_recovery_reader_stays_open":True}
