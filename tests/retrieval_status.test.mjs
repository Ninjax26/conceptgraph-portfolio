import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

// No test framework dependency: exercise the same pure helper used by the UI.
const source = await readFile(new URL("../src/lib/retrievalStatus.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
const { graphEmptyState, retrievalSummary } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const base = { total_nodes: 0, total_edges: 0, displayed_nodes: 0, displayed_edges: 0,
  graph_status: "GRAPH_PARTIAL", filter_reason: "query_subgraph", retrieval_mode: "two_hop" };

for (const [name, metadata, available, status, title] of [
  ["offline service", base, false, "GRAPH_READY", "Graph service offline"],
  ["timeout is not an outage", { ...base, fallback_reason: "graph_timeout", retrieval_mode: "vector_only" }, true, "GRAPH_READY", "Graph lookup took too long"],
  ["failed lookup preserves evidence", { ...base, fallback_reason: "graph_unavailable", retrieval_mode: "vector_only" }, true, "GRAPH_READY", "Answering from PDF passages"],
  ["missing projection is not waiting", { ...base, projection_missing: true }, true, "GRAPH_READY", "Stored graph not found"],
  ["vector mode is intentional", { ...base, retrieval_mode: "vector_only" }, true, "GRAPH_READY", "Answering from PDF passages"],
  ["valid empty extraction", base, true, "READY_WITHOUT_GRAPH", "No validated graph was produced"],
  ["unknown readiness does not imply outage", undefined, null, undefined, "Your course graph will appear here"],
]) {
  test(name, () => {
    const result = graphEmptyState(metadata, available, status);
    assert.equal(result.title, title);
    assert.ok(result.detail.length > 20);
  });
}

test("summary distinguishes vector fallback, stored-data mismatch and visualization", () => {
  assert.match(retrievalSummary({ ...base, fallback_reason: "graph_timeout" }), /time budget/);
  assert.match(retrievalSummary({ ...base, fallback_reason: "graph_unavailable" }), /unavailable/);
  assert.match(retrievalSummary({ ...base, projection_missing: true }), /no matching stored graph/);
  assert.match(retrievalSummary({ ...base, filter_reason: "course_graph_visualization_fallback" }), /context only/);
  assert.equal(retrievalSummary(base), "Evidence retrieved with two hop.");
});
