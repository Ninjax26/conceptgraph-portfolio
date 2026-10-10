const MESSAGES: Record<string, string> = {
  malformed_json: "The response was not valid JSON.",
  invalid_schema: "The response did not match the expected graph format.",
  invalid_endpoints: "A relationship referenced a missing concept.",
  duplicate_node_ids: "Different concepts reused the same identifier.",
  unsupported_relationship_type: "A relationship used an unsupported type.",
  unknown_source: "Some concepts had source references that could not be verified.",
  batch_limit: "The response exceeded the permitted graph size.",
  truncated_response: "The response ended before the graph was complete.",
  no_concepts: "Processed; the provider returned no concepts.",
  quota: "Provider quota was exhausted; completed work was preserved.",
  application_budget: "The shared application request budget was reached; completed work was preserved. This is not proof that the provider's own quota is exhausted.",
  provider_unavailable: "A provider timed out or was temporarily unavailable.",
  configuration: "The provider configuration needs attention.",
  provider_request: "The provider rejected the request.",
  budget: "Deferred to the next processing run within the demo budget.",
  no_new_relationships: "The linking pass completed without adding relationships.",
};

export function graphDiagnosticMessages(result: Record<string, unknown> | null | undefined): string[] {
  const entries = result?.graph_batch_diagnostics;
  if (!Array.isArray(entries)) return [];
  const messages = entries.flatMap((entry: unknown) => {
    if (!entry || typeof entry !== "object") return [];
    const item = entry as Record<string, unknown>;
    const message = typeof item.category === "string" && Object.prototype.hasOwnProperty.call(MESSAGES, item.category)
      ? MESSAGES[item.category] : undefined;
    if (!message) return [];
    const label = item.kind === "global" ? "Relationship linking"
      : typeof item.section_label === "string" ? item.section_label.slice(0, 160) : "Section";
    return [`${label}: ${message}`];
  });
  return [...new Set(messages)];
}
