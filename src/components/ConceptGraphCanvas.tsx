import cytoscape, {
  type Core,
  type EdgeDefinition,
  type EdgeSingular,
  type NodeDefinition,
  type NodeSingular,
} from "cytoscape";
import {
  Eye,
  EyeOff,
  ExternalLink,
  Link2,
  Maximize2,
  Minus,
  Plus,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

export interface GraphCanvasNode {
  id: string;
  label: string;
  type?: string;
  description?: string;
  documentName?: string;
  pageNumber?: number | null;
  sectionHeading?: string;
  uploadId?: string;
  retrievalHop?: 0 | 1 | 2;
}

export interface GraphCanvasEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
  relationType?: string;
  retrievalHop?: 1 | 2;
}

interface ConceptGraphCanvasProps {
  nodes: GraphCanvasNode[];
  edges: GraphCanvasEdge[];
  onOpenSource?: (node: GraphCanvasNode) => void;
}

const RELATION_COLORS: Record<string, string> = {
  PREREQUISITE_OF: "#0f766e",
  PART_OF: "#7c3aed",
  EXPLAINS: "#0284c7",
  RELATED_TO: "#64748b",
  CAUSES: "#dc2626",
  APPLIES_TO: "#d97706",
};

function relationLabel(value?: string): string {
  return (value || "RELATED_TO")
    .toLowerCase()
    .split("_")
    .join(" ");
}

export default function ConceptGraphCanvas({
  nodes,
  edges,
  onOpenSource,
}: ConceptGraphCanvasProps): JSX.Element {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphCanvasNode | null>(null);
  const [showUnlinked, setShowUnlinked] = useState(false);

  const connectedNodeIds = useMemo(() => {
    const ids = new Set<string>();
    edges.forEach((edge) => {
      ids.add(edge.source);
      ids.add(edge.target);
    });
    nodes.forEach((node) => {
      if (node.retrievalHop === 0) ids.add(node.id);
    });
    return ids;
  }, [edges, nodes]);

  const unlinkedCount = Math.max(0, nodes.length - connectedNodeIds.size);
  const visibleNodes = useMemo(
    () => edges.length === 0 || showUnlinked
      ? nodes
      : nodes.filter((node) => connectedNodeIds.has(node.id)),
    [connectedNodeIds, edges.length, nodes, showUnlinked],
  );
  const visibleNodeIds = useMemo(
    () => new Set(visibleNodes.map((node) => node.id)),
    [visibleNodes],
  );
  const visibleEdges = useMemo(
    () => edges.filter(
      (edge) => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target),
    ),
    [edges, visibleNodeIds],
  );
  const nodeById = useMemo(
    () => new Map(nodes.map((node) => [node.id, node])),
    [nodes],
  );
  const nodeByIdRef = useRef(nodeById);
  nodeByIdRef.current = nodeById;
  const selectedRelationships = useMemo(
    () => selectedNode
      ? edges.filter(
        (edge) => edge.source === selectedNode.id || edge.target === selectedNode.id,
      )
      : [],
    [edges, selectedNode],
  );
  const visibleRelationTypes = useMemo(
    () => [...new Set(
      visibleEdges.map((edge) => edge.relationType || edge.label || "RELATED_TO"),
    )].sort(),
    [visibleEdges],
  );
  const isHierarchy = visibleEdges.length > 0 && visibleEdges.every((edge) =>
    ["PREREQUISITE_OF", "PART_OF"].includes(
      edge.relationType || edge.label || "",
    ),
  );

  useEffect(() => {
    if (!containerRef.current) return undefined;

    const cy = cytoscape({
      container: containerRef.current,
      elements: [],
      minZoom: 0.28,
      maxZoom: 2.6,
      boxSelectionEnabled: false,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#ffffff",
            "border-color": "#94a3b8",
            "border-width": 2,
            color: "#172033",
            "font-family": "Inter, ui-sans-serif, system-ui, sans-serif",
            "font-size": 12,
            "font-weight": 600,
            height: "52px",
            label: "data(label)",
            "min-zoomed-font-size": 8,
            "overlay-opacity": 0,
            shape: "round-rectangle",
            "text-halign": "center",
            "text-max-width": "126px",
            "text-valign": "center",
            "text-wrap": "wrap",
            width: "148px",
          },
        },
        {
          selector: "node[retrievalHop = 0]",
          style: {
            "background-color": "#0f766e",
            "border-color": "#115e59",
            "border-width": 3,
            color: "#ffffff",
          },
        },
        {
          selector: "node[retrievalHop = 1]",
          style: {
            "background-color": "#ecfdf5",
            "border-color": "#10b981",
            "border-width": 3,
          },
        },
        {
          selector: "node[retrievalHop = 2]",
          style: {
            "background-color": "#eff6ff",
            "border-color": "#60a5fa",
          },
        },
        {
          selector: "node[isUnlinked = 1]",
          style: {
            "border-style": "dashed",
            opacity: 0.72,
          },
        },
        {
          selector: "edge",
          style: {
            "arrow-scale": 0.9,
            "curve-style": "bezier",
            "font-family": "Inter, ui-sans-serif, system-ui, sans-serif",
            "font-size": 9,
            "font-weight": 600,
            label: "data(displayLabel)",
            "line-color": "#94a3b8",
            "min-zoomed-font-size": 7,
            "overlay-opacity": 0,
            "target-arrow-color": "#94a3b8",
            "target-arrow-shape": "triangle",
            "text-background-color": "#ffffff",
            "text-background-opacity": 0.9,
            "text-background-padding": "4px",
            "text-border-color": "#e2e8f0",
            "text-border-opacity": 0.9,
            "text-border-width": 1,
            width: 2.2,
          },
        },
        {
          selector: 'edge[relationType = "PREREQUISITE_OF"]',
          style: {
            "line-color": RELATION_COLORS.PREREQUISITE_OF,
            "target-arrow-color": RELATION_COLORS.PREREQUISITE_OF,
          },
        },
        {
          selector: 'edge[relationType = "PART_OF"]',
          style: {
            "line-color": RELATION_COLORS.PART_OF,
            "target-arrow-color": RELATION_COLORS.PART_OF,
          },
        },
        {
          selector: 'edge[relationType = "EXPLAINS"]',
          style: {
            "line-color": RELATION_COLORS.EXPLAINS,
            "target-arrow-color": RELATION_COLORS.EXPLAINS,
          },
        },
        {
          selector: 'edge[relationType = "CAUSES"]',
          style: {
            "line-color": RELATION_COLORS.CAUSES,
            "target-arrow-color": RELATION_COLORS.CAUSES,
          },
        },
        {
          selector: 'edge[relationType = "APPLIES_TO"]',
          style: {
            "line-color": RELATION_COLORS.APPLIES_TO,
            "target-arrow-color": RELATION_COLORS.APPLIES_TO,
          },
        },
        {
          selector: 'edge[relationType = "RELATED_TO"]',
          style: {
            "line-color": RELATION_COLORS.RELATED_TO,
            "line-style": "dashed",
            "target-arrow-shape": "none",
          },
        },
        {
          selector: "edge[retrievalHop = 2]",
          style: {
            "line-style": "dashed",
          },
        },
        {
          selector: "node.selectedPath",
          style: {
            "background-color": "#ccfbf1",
            "border-color": "#0f766e",
            "border-width": 4,
            color: "#134e4a",
            opacity: 1,
          },
        },
        {
          selector: "edge.selectedPath",
          style: {
            "line-color": "#0f766e",
            "target-arrow-color": "#0f766e",
            opacity: 1,
            width: 4,
          },
        },
        {
          selector: ".dimmed",
          style: { opacity: 0.16 },
        },
      ],
    });

    cyRef.current = cy;
    cy.on("tap", "node", (event) => {
      const selected = event.target as NodeSingular;
      setSelectedNode(nodeByIdRef.current.get(selected.id()) ?? null);
      cy.elements().removeClass("selectedPath dimmed").addClass("dimmed");
      selected.removeClass("dimmed").addClass("selectedPath");

      selected.connectedEdges().forEach((edge) => {
        edge.removeClass("dimmed").addClass("selectedPath");
        edge.connectedNodes().removeClass("dimmed").addClass("selectedPath");
      });

      let frontier: NodeSingular[] = [selected];
      const visited = new Set([selected.id()]);
      for (let depth = 0; depth < 2; depth += 1) {
        const next: NodeSingular[] = [];
        frontier.forEach((node) => {
          node.incomers('edge[relationType = "PREREQUISITE_OF"]').forEach((item) => {
            const edge = item as EdgeSingular;
            const prerequisite = edge.source();
            edge.removeClass("dimmed").addClass("selectedPath");
            prerequisite.removeClass("dimmed").addClass("selectedPath");
            if (!visited.has(prerequisite.id())) {
              visited.add(prerequisite.id());
              next.push(prerequisite);
            }
          });
        });
        frontier = next;
      }
    });
    cy.on("tap", (event) => {
      if (event.target === cy) {
        cy.elements().removeClass("selectedPath dimmed");
        setSelectedNode(null);
      }
    });

    let resizeFrame = 0;
    const resizeObserver = new ResizeObserver(() => {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(() => cy.resize());
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      cancelAnimationFrame(resizeFrame);
      cy.stop();
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.stop();
    cy.elements().remove();
    setSelectedNode(null);
    if (visibleNodes.length === 0) return;

    const elements: Array<NodeDefinition | EdgeDefinition> = [
      ...visibleNodes.map((node) => ({
        data: {
          ...node,
          isUnlinked: connectedNodeIds.has(node.id) ? 0 : 1,
        },
      })),
      ...visibleEdges.map((edge) => {
        const relationType = edge.relationType || edge.label || "RELATED_TO";
        return {
          data: {
            ...edge,
            relationType,
            displayLabel: isHierarchy ? "" : relationLabel(relationType),
          },
        };
      }),
    ];

    cy.add(elements);
    cy.resize();
    const layout = cy.layout(
      isHierarchy
        ? {
          name: "breadthfirst",
          animate: false,
          avoidOverlap: true,
          circle: false,
          directed: true,
          fit: true,
          nodeDimensionsIncludeLabels: true,
          padding: 44,
          spacingFactor: 1.25,
        }
        : visibleEdges.length > 0
        ? {
          name: "cose",
          animate: visibleNodes.length <= 24,
          animationDuration: 420,
          componentSpacing: 110,
          fit: true,
          gravity: 0.35,
          idealEdgeLength: 170,
          nestingFactor: 0.8,
          nodeDimensionsIncludeLabels: true,
          nodeRepulsion: 11_000,
          numIter: 1_100,
          padding: 64,
          randomize: true,
        }
        : {
          name: "grid",
          animate: false,
          avoidOverlap: true,
          condense: false,
          fit: true,
          padding: 64,
          spacingFactor: 1.35,
        },
    );
    layout.run();
  }, [connectedNodeIds, isHierarchy, visibleEdges, visibleNodes]);

  const clearSelection = (): void => {
    setSelectedNode(null);
    cyRef.current?.elements().removeClass("selectedPath dimmed");
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-white px-3 py-2.5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-700">
            <Link2 className="h-3.5 w-3.5 text-teal-700" />
            {visibleNodes.length} concepts · {visibleEdges.length} relationships
          </span>
          {visibleRelationTypes.map((type) => (
            <span className="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500" key={type}>
              <span
                aria-hidden="true"
                className="h-1.5 w-3 rounded-full"
                style={{ backgroundColor: RELATION_COLORS[type] || "#64748b" }}
              />
              {relationLabel(type)}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-1">
          {unlinkedCount > 0 && edges.length > 0 ? (
            <button
              aria-pressed={showUnlinked}
              className="mr-1 inline-flex h-8 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 text-[11px] font-semibold text-slate-600 transition hover:border-teal-300 hover:text-teal-800"
              onClick={() => {
                clearSelection();
                setShowUnlinked((value) => !value);
              }}
              type="button"
            >
              {showUnlinked ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              {showUnlinked ? "Hide" : "Show"} {unlinkedCount} unlinked
            </button>
          ) : null}
          <GraphControl label="Zoom out" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() * 0.82)}>
            <Minus className="h-3.5 w-3.5" />
          </GraphControl>
          <GraphControl label="Zoom in" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() * 1.18)}>
            <Plus className="h-3.5 w-3.5" />
          </GraphControl>
          <GraphControl label="Fit graph to view" onClick={() => cyRef.current?.fit(undefined, 56)}>
            <Maximize2 className="h-3.5 w-3.5" />
          </GraphControl>
        </div>
      </div>

      <div className="relative min-h-0 flex-1 bg-[radial-gradient(circle_at_1px_1px,_#dbe5ea_1px,_transparent_0)] [background-size:22px_22px]">
        <div
          aria-label={`Interactive concept graph with ${visibleNodes.length} concepts and ${visibleEdges.length} relationships`}
          className="absolute inset-0"
          ref={containerRef}
          role="img"
        />
        {visibleEdges.length === 0 ? (
          <div className="pointer-events-none absolute bottom-3 left-1/2 z-10 -translate-x-1/2 rounded-full border border-amber-200 bg-amber-50/95 px-3 py-1.5 text-center text-[11px] font-medium text-amber-800 shadow-sm">
            No validated relationships were extracted; these concepts remain searchable.
          </div>
        ) : null}
        {selectedNode ? (
          <aside className="absolute bottom-3 left-3 right-3 z-10 max-h-[62%] overflow-auto rounded-xl border border-slate-200 bg-white/97 p-4 shadow-xl backdrop-blur sm:right-auto sm:w-[360px]">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-ink">{selectedNode.label}</p>
                <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide">
                  <span className="rounded-full bg-teal-50 px-2 py-0.5 text-teal-700">{selectedNode.type || "concept"}</span>
                  {selectedNode.retrievalHop !== undefined ? (
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">
                      {selectedNode.retrievalHop === 0
                        ? "query match"
                        : selectedNode.retrievalHop === 1
                          ? "1-hop prerequisite"
                          : "2-hop prerequisite"}
                    </span>
                  ) : null}
                </div>
              </div>
              <button
                aria-label="Close concept details"
                className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
                onClick={clearSelection}
                type="button"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <p className="mt-3 text-xs leading-5 text-slate-600">
              {selectedNode.description || "No description was extracted for this concept."}
            </p>

            {selectedRelationships.length > 0 ? (
              <div className="mt-3 border-t border-slate-100 pt-3">
                <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Relationships</p>
                <div className="mt-1.5 grid gap-1.5">
                  {selectedRelationships.slice(0, 5).map((edge) => {
                    const outgoing = edge.source === selectedNode.id;
                    const otherNode = nodeById.get(outgoing ? edge.target : edge.source);
                    return (
                      <div className="flex items-center gap-2 text-xs text-slate-600" key={edge.id}>
                        <span
                          className="h-1.5 w-1.5 shrink-0 rounded-full"
                          style={{ backgroundColor: RELATION_COLORS[edge.relationType || edge.label || ""] || "#64748b" }}
                        />
                        <span className="font-medium text-slate-700">{outgoing ? "→" : "←"} {relationLabel(edge.relationType || edge.label)}</span>
                        <span className="truncate">{otherNode?.label || "Unknown concept"}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : null}

            <div className="mt-3 rounded-lg bg-slate-50 p-2.5 text-xs text-slate-600">
              <p className="font-semibold text-slate-700">
                {selectedNode.documentName || "Source PDF unavailable"}
              </p>
              <p className="mt-1">
                {selectedNode.pageNumber ? `Page ${selectedNode.pageNumber}` : "Page unavailable"}
                {selectedNode.sectionHeading ? ` · ${selectedNode.sectionHeading}` : ""}
              </p>
            </div>
            {selectedNode.uploadId && onOpenSource ? (
              <button
                className="mt-3 inline-flex items-center gap-2 rounded-lg bg-teal-700 px-3 py-2 text-xs font-semibold text-white transition hover:bg-teal-800"
                onClick={() => onOpenSource(selectedNode)}
                type="button"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                Open source page
              </button>
            ) : null}
          </aside>
        ) : null}
      </div>
    </div>
  );
}

function GraphControl({
  children,
  label,
  onClick,
}: {
  children: JSX.Element;
  label: string;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      aria-label={label}
      className="grid h-8 w-8 place-items-center rounded-lg border border-slate-200 bg-white text-slate-600 transition hover:border-teal-300 hover:bg-teal-50 hover:text-teal-800"
      onClick={onClick}
      title={label}
      type="button"
    >
      {children}
    </button>
  );
}
