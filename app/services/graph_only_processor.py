"""One-claim orchestration for explicitly gated idle workers; production defaults off."""
from app.core.blocking import run_async_safely
from app.core.config import settings
from app.services.graph_continuation_service import GraphContinuationService, GraphJobClaim
from app.services.graph_job_extraction import GraphJobAuthorityLost, GraphJobExtraction
from app.services.resource_budget_service import ResourceBudgetService


class GraphJobNotPublishable(RuntimeError):
    """No publication was started; retain the bounded claim for lease recovery."""


class GraphOnlyProcessor:
    def __init__(self, sessions, ingestion, *, config=None, jobs=None, extractor=None, budgets=None):
        self.sessions, self.ingestion = sessions, ingestion
        self.config = settings if config is None else config
        self.jobs = GraphContinuationService() if jobs is None else jobs
        self.extractor = (GraphJobExtraction(sessions, ingestion, jobs=self.jobs)
                          if extractor is None else extractor)
        self.budgets = ResourceBudgetService(self.config) if budgets is None else budgets

    async def process(self, claim: GraphJobClaim, *, lease_seconds: int = 180) -> bool:
        """Run exactly one existing claim, preserving READY identity and source/vector data.

        The caller owns claiming and scheduling. Provider deferrals propagate from
        extraction; other failures leave bounded lease recovery, never a hot loop.
        Saved validated drafts resume through marker/receipt recovery without new
        provider work. The existing publisher drains its entire fenced completion
        unit on cancellation. The coordinator may call it in a gated idle worker slot.
        """
        if (self.config.graph_projection_fencing_enabled is not True
            or self.config.graph_continuation_snapshots_enabled is not True
            or self.config.processing_role not in {"combined", "worker"}):
            raise RuntimeError("Graph-only processing requires explicitly enabled private/qualified worker configuration.")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid graph-job lease.")
        async with self.sessions() as session:
            try:
                if not await self.jobs.heartbeat(session, claim, lease_seconds=lease_seconds):
                    raise GraphJobAuthorityLost("Graph job lease was lost before processing.")
                candidate = await self.jobs.load_publication_draft(session, claim)
            finally:
                await session.rollback()  # Release draft-read document/job locks before extraction.

        if candidate is None:
            graph = await self.extractor.extract(claim, lease_seconds=lease_seconds)
            if graph.provider_limited:
                # A known deadline is already deferred by GraphJobExtraction.
                # Do not persist a permanently unpublishable immutable draft when
                # the provider supplied no usable deadline.
                raise GraphJobNotPublishable("Provider-limited graph has no persisted retry deadline.")

            async def stage():
                async with self.sessions() as session:
                    # Installation accounting precedes document -> job -> draft locks.
                    await self.budgets.lock(session)
                    prepared = await self.jobs.prepare_publication(session, claim, graph)
                    if prepared is None:
                        raise GraphJobAuthorityLost("Graph job was superseded before staging.")
                    if not await self.jobs.save_publication_draft(session, claim, prepared):
                        raise GraphJobNotPublishable("Draft capacity or current claim no longer permits staging.")
                    await session.commit()
            await run_async_safely(stage())

        # Publisher revalidates persisted source/job/draft, commits its intent,
        # observes the remote marker and drains the final receipt/result commit.
        if not await self.jobs.publish_publication(self.sessions, claim, self.ingestion):
            raise GraphJobAuthorityLost("Graph job no longer permits publication.")
        return True
