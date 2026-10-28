"""Shared graph-only result fields; source and processing metrics stay caller-owned."""

from typing import Any

from app.core.processing import assess_graph_status
from app.schemas.extraction import GraphExtractionResponse


def graph_result_metadata(graph: GraphExtractionResponse) -> dict[str, Any]:
    graph_status = assess_graph_status(
        len(graph.nodes),
        len(graph.relationships),
        sections_total=graph.sections_total,
        sections_succeeded=graph.sections_succeeded,
        sections_empty=graph.sections_empty,
        batches_failed=graph.batches_failed,
        batches_skipped=graph.batches_skipped,
    ).value
    return {
        "sections_scanned_locally": graph.sections_total,
        "nodes_upserted": len(graph.nodes),
        "relationships_upserted": len(graph.relationships),
        "graph_status": graph_status,
        "graph_sections_total": graph.sections_total,
        "graph_sections_succeeded": graph.sections_succeeded,
        "graph_sections_failed": graph.sections_failed,
        "graph_sections_processed": graph.sections_processed,
        "graph_sections_empty": graph.sections_empty,
        "graph_batches_empty": graph.batches_empty,
        "graph_batches_total": graph.batches_total,
        "graph_batches_succeeded": graph.batches_succeeded,
        "graph_batches_failed": graph.batches_failed,
        "graph_batches_skipped": graph.batches_skipped,
        "graph_provider_limited": graph.provider_limited,
        "graph_extraction_budget_applied": graph.extraction_budget_applied,
        "graph_global_linking_attempted": graph.global_linking_attempted,
        "graph_global_linking_succeeded": graph.global_linking_succeeded,
        "graph_global_linking_processed": graph.global_linking_processed,
        "graph_relationship_pass_version": 4 if graph.global_linking_attempted else 0,
        "graph_checkpointed_batches": graph.batches_succeeded,
        "graph_failed_sections": graph.failed_section_labels,
        "graph_batch_diagnostics": [entry.model_dump(mode="json") for entry in graph.batch_diagnostics],
    }
