import type {
  GraphCanvasEdge,
  GraphCanvasNode,
} from "@/components/ConceptGraphCanvas";
import type { ConceptNode, GraphContextItem } from "@/services/api";

export interface GraphElements {
  nodes: GraphCanvasNode[];
  edges: GraphCanvasEdge[];
  omittedRelationships: number;
}

function mergeNode(
  current: GraphCanvasNode | undefined,
  next: GraphCanvasNode,
): GraphCanvasNode {
  if (!current) return next;

  const hops = [current.retrievalHop, next.retrievalHop].filter(
    (hop): hop is 0 | 1 | 2 => hop !== undefined,
  );
  return {
    id: current.id,
    label: next.label || current.label,
    type: next.type || current.type,
    description: next.description || current.description,
    documentName: next.documentName || current.documentName,
    pageNumber: next.pageNumber ?? current.pageNumber,
    sectionHeading: next.sectionHeading || current.sectionHeading,
    uploadId: next.uploadId || current.uploadId,
    retrievalHop: hops.length > 0
      ? Math.min(...hops) as 0 | 1 | 2
      : undefined,
  };
}

function toCanvasNode(
  concept: Partial<ConceptNode>,
  fallbackId: string,
): GraphCanvasNode {
  const id = String(concept.id || fallbackId);
  return {
    id,
    label: concept.name || id,
    type: concept.type,
    description: concept.description,
    documentName: concept.document_name,
    pageNumber: concept.page_number,
    sectionHeading: concept.section_heading,
    uploadId: concept.upload_id,
    retrievalHop: concept.retrieval_hop,
  };
}

function nodeCandidates(item: GraphContextItem): Array<Partial<ConceptNode>> {
  const candidates = [
    item.concept,
    ...(item.related_concepts ?? []),
    ...(item.prerequisites ?? []),
    ...(item.one_hop_prerequisites ?? []),
    ...(item.two_hop_prerequisites ?? []),
  ];
  const seen = new Set<string>();
  return candidates.filter((node, index) => {
    const key = String(node.id || `anonymous-${index}`);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** Build every node before resolving edges; Neo4j may return endpoints in later records. */
export function buildGraphElements(graphContext: GraphContextItem[]): GraphElements {
  const nodes = new Map<string, GraphCanvasNode>();
  const edges = new Map<string, GraphCanvasEdge>();

  graphContext.forEach((item, itemIndex) => {
    nodeCandidates(item).forEach((concept, conceptIndex) => {
      const next = toCanvasNode(concept, `concept-${itemIndex}-${conceptIndex}`);
      nodes.set(next.id, mergeNode(nodes.get(next.id), next));
    });
  });

  let omittedRelationships = 0;
  graphContext.forEach((item) => {
    (item.relationships ?? []).forEach((relationship) => {
      const source = String(relationship.source || "").trim();
      const target = String(relationship.target || "").trim();
      const relationType = String(relationship.type || "RELATED_TO")
        .trim()
        .toUpperCase();
      if (!source || !target || source === target || !nodes.has(source) || !nodes.has(target)) {
        omittedRelationships += 1;
        return;
      }
      const edgeId = `${source}->${target}:${relationType}`;
      const sourceHop = nodes.get(source)?.retrievalHop;
      const targetHop = nodes.get(target)?.retrievalHop;
      const strongestHop = Math.max(sourceHop ?? 0, targetHop ?? 0);
      edges.set(edgeId, {
        id: edgeId,
        source,
        target,
        label: relationType,
        relationType,
        retrievalHop: strongestHop === 1 || strongestHop === 2
          ? strongestHop
          : undefined,
      });
    });
  });

  return {
    nodes: [...nodes.values()],
    edges: [...edges.values()],
    omittedRelationships,
  };
}
