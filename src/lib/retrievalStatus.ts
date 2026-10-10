import type { GraphStatus, QueryResponse } from "../services/api";

type GraphMetadata = QueryResponse["graph_metadata"];

export function retrievalSummary(metadata: GraphMetadata): string {
  if (metadata.fallback_reason === "graph_timeout") return "Graph lookup exceeded its time budget; evidence was retrieved with vector search.";
  if (metadata.fallback_reason) return "Graph lookup was unavailable; evidence was retrieved with vector search.";
  if (metadata.projection_missing) return "The selected database has no matching stored graph; evidence was retrieved from PDF passages.";
  if (metadata.filter_reason === "course_graph_visualization_fallback") return "No query concepts matched. Evidence came from PDF passages; the course graph is shown for context only.";
  return `Evidence retrieved with ${(metadata.retrieval_mode ?? "two_hop").split("_").join(" ")}.`;
}

export function graphEmptyState(metadata: GraphMetadata | undefined, available: boolean | null, status: GraphStatus | null | undefined) {
  if (available === false) return {
    title: "Graph service offline",
    detail: "Neo4j is unavailable right now. Existing relationships cannot load, and new graph builds should wait until the service is restored.",
  };
  if (metadata?.fallback_reason === "graph_timeout") return {
    title: "Graph lookup took too long",
    detail: "Vector search was used instead. Your answer and citations remain available; you can retry the graph lookup later.",
  };
  if (metadata?.fallback_reason) return {
    title: "Answering from PDF passages",
    detail: "The graph lookup failed. Your answer and source citations are still available in the answer panel.",
  };
  if (metadata?.projection_missing) return {
    title: "Stored graph not found",
    detail: "This PDF has recorded graph counts, but its concepts are missing from the selected Neo4j database. PDF passages remain searchable. Check the database configuration or restore the affected document's graph.",
  };
  if (metadata?.retrieval_mode === "vector_only") return {
    title: "Answering from PDF passages",
    detail: "Vector mode does not retrieve a graph. Choose One hop or Two hops and run your question to explore concept connections.",
  };
  if (status === "READY_WITHOUT_GRAPH") return {
    title: "No validated graph was produced",
    detail: "The PDF is still searchable for grounded answers, but the extracted graph did not meet the minimum quality checks.",
  };
  return {
    title: "Your course graph will appear here",
    detail: "Select a ready course and ask a question to reveal relevant concepts, relationships, and prerequisites.",
  };
}
