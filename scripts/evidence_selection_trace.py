"""Offline adapters observe actual calls; no extra search or model invocation."""
from app.services.evidence_trace import EvidenceTrace
from app.services.rag_service import RetrievalService
from app.services.rerank_service import RerankService


class TracingVectorClient:
    def __init__(self, client, trace: EvidenceTrace):
        self.client, self.trace = client, trace

    def __getattr__(self, name):
        return getattr(self.client, name)

    def query_points(self, **kwargs):
        result = self.client.query_points(**kwargs)
        self._record("dense_search", result.points)
        return result

    def scroll(self, **kwargs):
        result = self.client.scroll(**kwargs)
        self._record("identifier_lookup", result[0])
        return result

    def _record(self, stage, points):
        for rank, point in enumerate(points, 1):
            chunk = RetrievalService._point_to_chunk(point)
            self.trace.record(stage, "candidate", chunk, rank=rank, vector_score=chunk["score"])


class TracingRerankService(RerankService):
    def __init__(self, trace: EvidenceTrace):
        super().__init__()
        self.trace = trace
        self.group = 0

    def _rank_windows(self, chunks, windows, scores):
        scores = list(scores)
        ranked = super()._rank_windows(chunks, windows, scores)
        # First maximum wins, matching the real reducer's tie behavior. The
        # reducer above remains the sole producer of the returned ranking.
        winners = {}
        for index, (chunk_index, _) in enumerate(windows):
            if chunk_index not in winners or scores[index] > scores[winners[chunk_index]]:
                winners[chunk_index] = index
        for index, ((chunk_index, passage), score) in enumerate(zip(windows, scores, strict=True)):
            self.trace.record("reranking", "selected_window" if winners[chunk_index] == index else "lower_or_tied_window",
                              chunks[chunk_index], passage=passage, rerank_score=float(score),
                              group=self.group, window_index=index)
        self.group += 1
        return ranked
