"""Bounded, explicitly gated exhausted-publication recovery and body retention."""
import asyncio
import logging
from time import monotonic

from app.core.blocking import run_async_safely
from app.core.config import settings
from app.services.graph_continuation_service import GraphContinuationService
from app.services.resource_budget_service import ResourceBudgetService

logger = logging.getLogger(__name__)


class GraphJobMaintenance:
    def __init__(self, sessions, ingestion, *, config=None, jobs=None, budgets=None, clock=monotonic):
        self.sessions, self.ingestion = sessions, ingestion
        self.config = settings if config is None else config
        self.jobs = GraphContinuationService() if jobs is None else jobs
        self.budgets = ResourceBudgetService(self.config) if budgets is None else budgets
        self.clock = clock
        self._lock = asyncio.Lock()
        self._next_due = 0.
        self._after_intent_id = None

    async def run_if_due(self) -> bool:
        config = self.config
        if (config.graph_continuation_maintenance_enabled is not True
            or config.graph_continuation_dispatch_enabled is not True
            or config.graph_continuation_snapshots_enabled is not True
            or config.graph_projection_fencing_enabled is not True
            or config.processing_role not in {'worker', 'combined'} or self._lock.locked()):
            return False
        async with self._lock:
            now = self.clock()
            if now < self._next_due:
                return False
            # Failures must not turn an idle worker into a hot database/remote loop.
            self._next_due = now + config.graph_maintenance_interval_seconds
            limit = config.graph_maintenance_batch_size
            async with self.sessions() as session:
                ids = await self.jobs.list_exhausted_publications(session, limit=limit,
                    after_intent_id=self._after_intent_id)
            self._after_intent_id = ids[-1] if ids else None
            # Discovery owns no locks/sessions across remote observation. Each
            # reconciler revalidates the current source/job/draft and drains its
            # complete receipt transaction; it never extracts or writes a graph.
            for intent_id in ids:
                try:
                    await self.jobs.reconcile_exhausted_publication(self.sessions, intent_id, self.ingestion)
                except Exception:
                    logger.exception('Exhausted graph publication could not be reconciled intent=%s', intent_id)

            async def prune():
                async with self.sessions() as session:
                    await self.budgets.lock(session)
                    await self.jobs.prune_terminal_drafts(session,
                        older_than_days=config.graph_terminal_retention_days, limit=limit)
                    await self.jobs.prune_terminal_snapshots(session,
                        older_than_days=config.graph_terminal_retention_days, limit=limit)
                    await session.commit()
            await run_async_safely(prune())
            return True
