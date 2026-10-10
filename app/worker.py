"""Run ingestion separately: PROCESSING_ROLE=worker python -m app.worker."""

import asyncio
import contextlib
import logging
import signal

from app.core.config import settings
from app.core.database import close_database_connections, initialize_database_schema
from app.core.processing_coordinator import processing_coordinator
from app.services.demo_retention_service import demo_retention_service
from app.services.document_processing_service import document_processing_service
from app.services.worker_presence_service import remove_worker_presence, renew_worker_presence


async def run_worker():
    if settings.processing_role != "worker" or not settings.shared_provider_limits_enabled:
        raise RuntimeError("Set PROCESSING_ROLE=worker and SHARED_PROVIDER_LIMITS_ENABLED=true.")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await initialize_database_schema()
        await asyncio.to_thread(document_processing_service.ingestion_service.validate_qdrant_collection)
        await renew_worker_presence(processing_coordinator.instance_id, settings.processing_concurrency,
                                    settings.processing_lease_seconds)
        await processing_coordinator.start()
        await demo_retention_service.start()
        while not stopped.is_set():
            await renew_worker_presence(processing_coordinator.instance_id, settings.processing_concurrency,
                                        settings.processing_lease_seconds)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=settings.processing_heartbeat_seconds)
            except TimeoutError:
                pass
    finally:
        await demo_retention_service.stop()
        await processing_coordinator.stop()
        with contextlib.suppress(Exception):
            await remove_worker_presence(processing_coordinator.instance_id)
        await close_database_connections()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
