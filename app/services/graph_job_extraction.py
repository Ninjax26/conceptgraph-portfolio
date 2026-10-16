"""Internal extraction stage only: no dispatch, projection or job completion."""

import asyncio
from contextlib import suppress

from app.core.blocking import run_async_safely
from app.services.graph_continuation_service import GraphContinuationService, GraphJobClaim
from app.services.ingestion_service import IngestionService
from app.schemas.extraction import GraphExtractionResponse


class GraphJobAuthorityLost(RuntimeError):
    """The independent graph-job fence no longer authorizes this work."""


class GraphJobDeferred(RuntimeError):
    """Quota-limited work was durably deferred; do not publish this attempt."""


class GraphJobExtraction:
    def __init__(self, sessions, ingestion: IngestionService, *, jobs=None):
        self.sessions = sessions
        self.ingestion = ingestion
        self.jobs = jobs if jobs is not None else GraphContinuationService()

    async def extract(self, claim: GraphJobClaim, *, lease_seconds: int = 180) -> GraphExtractionResponse:
        """Resume from authoritative inputs; caller must separately fence publication.

        No document task/status/vector/source writes. Failed extraction leaves
        the durable claim for lease recovery, not an immediate retry loop.
        """
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid graph-job lease.")
        async with self.sessions() as session:
            inputs = await self.jobs.load_input(session, claim)
        if inputs is None:
            raise GraphJobAuthorityLost("Graph job is no longer eligible.")

        async def renew():
            async with self.sessions() as session:
                if not await self.jobs.heartbeat(session, claim, lease_seconds=lease_seconds):
                    raise GraphJobAuthorityLost("Graph job lease was lost.")

        async def save(batch, extraction):
            async with self.sessions() as session:
                if not await self.jobs.save_checkpoint(session, claim, batch=batch, extraction=extraction):
                    raise GraphJobAuthorityLost("Graph job checkpoint was fenced out.")

        async def maintain_lease():
            while True:
                await asyncio.sleep(lease_seconds / 3)
                await renew()

        # Renew before any provider work, not just at the first timer tick.
        await renew()
        extraction = asyncio.create_task(self.ingestion.extract_graph_from_chunks(
            inputs.chunks, completed_batches=inputs.checkpoints, on_batch_completed=save))
        heartbeat = asyncio.create_task(maintain_lease())
        async def drain(tasks):
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task

        try:
            done, _ = await asyncio.wait((extraction, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat  # Propagate lease loss/database failure, even if both finished.
            result = await extraction
            await renew()  # Returned output is not authority to publish later.
            if result.provider_limited and result.provider_retry_after_seconds is not None:
                # Deferral clears the claim; stop renewal before changing it.
                heartbeat.cancel()
                await run_async_safely(drain((heartbeat,)))
                async def defer():
                    async with self.sessions() as session:
                        return await self.jobs.defer(session, claim,
                            retry_after_seconds=result.provider_retry_after_seconds)
                if not await run_async_safely(defer()):
                    raise GraphJobAuthorityLost("Graph job deferral was fenced out.")
                raise GraphJobDeferred("Graph job deferred until its persisted retry deadline.")
            return result
        finally:
            for task in (extraction, heartbeat):
                task.cancel()
            await run_async_safely(drain((extraction, heartbeat)))
