import cytoscape, {
  type Core,
  type EdgeDefinition,
  type NodeDefinition,
  type NodeSingular,
  type EdgeSingular,
} from "cytoscape";
import { ExternalLink, Maximize2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

export interface GraphCanvasNode {
  id: string;
  label: string;
  type?: string;
  description?: string;
  documentName?: string;
  pageNumber?: number | null;
  sectionHeading?: string;
  uploadId?: string;
}

export interface GraphCanvasEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
}

interface ConceptGraphCanvasProps {
  nodes: GraphCanvasNode[];
  edges: GraphCanvasEdge[];
  onOpenSource?: (node: GraphCanvasNode) => void;
}

export default function ConceptGraphCanvas({
  nodes,
  edges,
  onOpenSource,
}: ConceptGraphCanvasProps): JSX.Element {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphCanvasNode | null>(null);

  // Initialize Cytoscape instance once
  useEffect(() => {
    if (!containerRef.current) {
      return undefined;
    }

    const cy = cytoscape({
      container: containerRef.current,
      elements: [],
      minZoom: 0.35,
      maxZoom: 2.4,
      layout: {
        name: "cose",
        animate: false,
        idealEdgeLength: 140,
        nodeRepulsion: 7000,
        nestingFactor: 0.8,
      },
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#ffffff",
            "border-color": "#2f6f73",
            "border-width": 2,
            color: "#172033",
            "font-size": 12,
            "font-weight": 600,
            height: "44px",
            label: "data(label)",
            "min-zoomed-font-size": 8,
            "overlay-opacity": 0,
            shape: "round-rectangle",
            "text-halign": "center",
            "text-max-width": "116px",
            "text-valign": "center",
            "text-wrap": "wrap",
            width: "132px",
          },
        },
        {
          selector: "edge",
          style: {
            "curve-style": "bezier",
            "font-size": 10,
            label: "data(label)",
            "line-color": "#a7b5c1",
            "target-arrow-color": "#a7b5c1",
            "target-arrow-shape": "triangle",
            "text-background-color": "#f7f9fb",
            "text-background-opacity": 0.8,
            "text-background-padding": "3px",
            width: 2,
          },
        },
        {
          selector: "node.selectedPath",
          style: {
            "background-color": "#dbf7f2",
            "border-color": "#0d9488",
            "border-width": 4,
            color: "#0f3f46",
          },
        },
        {
          selector: "edge.selectedPath",
          style: {
            "line-color": "#0d9488",
            "target-arrow-color": "#0d9488",
            width: 4,
          },
        },
        {
          selector: ".dimmed",
          style: {
            opacity: 0.28,
          },
        },
      ],
    });

    cyRef.current = cy;
    cy.userZoomingEnabled(true);
    cy.userPanningEnabled(true);
    cy.nodes().grabify();

    cy.on("tap", "node", async (event) => {
      const selected = event.target as NodeSingular;
      setSelectedNode({
        id: selected.id(),
        label: String(selected.data("label")),
        type: selected.data("type") ? String(selected.data("type")) : undefined,
        description: selected.data("description")
          ? String(selected.data("description"))
          : undefined,
        documentName: selected.data("documentName")
          ? String(selected.data("documentName"))
          : undefined,
        pageNumber: typeof selected.data("pageNumber") === "number"
          ? Number(selected.data("pageNumber"))
          : null,
        sectionHeading: selected.data("sectionHeading")
          ? String(selected.data("sectionHeading"))
          : undefined,
        uploadId: selected.data("uploadId")
          ? String(selected.data("uploadId"))
          : undefined,
      });

      // Reset and dim everything first
      cy.elements().removeClass("selectedPath dimmed");
      cy.elements().addClass("dimmed");

      // Animated traversal: follow prerequisite links back through the chain.
      // This makes the selected concept reveal what it depends on.
      try {
        const maxSteps = 50;
        let current: NodeSingular | null = selected;

        // mark start node
        current.removeClass("dimmed").addClass("selectedPath");

        for (let step = 0; step < maxSteps && current; step++) {
          const inEdges = current.incomers("edge").filter(
            (edge) => edge.data("label") === "PREREQUISITE_OF",
          );
          if (inEdges.length === 0) break;

          // Prefer an edge that leads to an unvisited prerequisite node
          const edge = (
            inEdges.filter((e) => !((e as EdgeSingular).source().hasClass("selectedPath")))[0] ??
            inEdges[0]
          ) as EdgeSingular;
          const targetNode = edge.source() as NodeSingular;

          // reveal and animate the edge
          edge.removeClass("dimmed");
          await new Promise<void>((resolve) => {
            // Use any cast because animate options typing varies between versions
            (edge as any).animate(
              {
                style: {
                  "line-color": "#0d9488",
                  "target-arrow-color": "#0d9488",
                  width: 4,
                },
              },
              { duration: 400, complete: () => resolve() }
            );
          });

          // lock-in selected style on edge and reveal the node
          edge.addClass("selectedPath");
          targetNode.removeClass("dimmed").addClass("selectedPath");

          // small pause before continuing to next hop
          await new Promise((r) => setTimeout(r, 150));

          // advance
          current = targetNode;
        }
      } catch (err) {
        // Fallback: instant highlight if animation fails
        const incomingEdges = selected.incomers("edge");
        const incomingNodes = selected.incomers("node");
        selected.removeClass("dimmed").addClass("selectedPath");
        incomingEdges.removeClass("dimmed").addClass("selectedPath");
        incomingNodes.removeClass("dimmed").addClass("selectedPath");
      }
    });

    cy.on("tap", (event) => {
      if (event.target === cy) {
        cy.elements().removeClass("selectedPath dimmed");
        setSelectedNode(null);
      }
    });

    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, []); // Run once on mount

  // Update data when nodes or edges change
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.elements().remove(); // Clear previous data
    setSelectedNode(null);

    if (nodes.length === 0 && edges.length === 0) {
      return;
    }

    const elements: Array<NodeDefinition | EdgeDefinition> = [
      ...nodes.map((node) => ({
        data: {
          id: node.id,
          label: node.label,
          type: node.type ?? "concept",
          description: node.description ?? "",
          documentName: node.documentName ?? "",
          pageNumber: node.pageNumber ?? null,
          sectionHeading: node.sectionHeading ?? "",
          uploadId: node.uploadId ?? "",
        },
      })),
      ...edges.map((edge) => ({
        data: {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          label: edge.label ?? "",
        },
      })),
    ];

    cy.add(elements);
    
    // Re-run layout to position new nodes correctly
    cy.layout({
      name: "cose",
      animate: true,
      idealEdgeLength: 140,
      nodeRepulsion: 7000,
      nestingFactor: 0.8,
    }).run();
    
    // Fit viewport to graph
    cy.fit(undefined, 50);

  }, [nodes, edges]);

  return (
    <div className="relative h-full min-h-[480px] overflow-hidden rounded-md border border-slate-200 bg-panel">
      {nodes.length === 0 ? (
        <div className="flex h-full min-h-[480px] items-center justify-center px-8 text-center text-sm text-slate-500">
          Ask a question to load the conceptual prerequisite map.
        </div>
      ) : null}
      <div ref={containerRef} className="absolute inset-0" />
      {nodes.length > 0 ? (
        <button
          aria-label="Fit graph to view"
          className="absolute right-3 top-3 z-10 grid h-9 w-9 place-items-center rounded-md border border-slate-200 bg-white text-slate-600 shadow-sm hover:bg-slate-50"
          onClick={() => cyRef.current?.fit(undefined, 48)}
          type="button"
        >
          <Maximize2 className="h-4 w-4" />
        </button>
      ) : null}
      {selectedNode ? (
        <aside className="absolute bottom-3 left-3 right-3 z-10 rounded-md border border-slate-200 bg-white/95 p-3 shadow-lg backdrop-blur sm:right-auto sm:max-w-sm">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-ink">{selectedNode.label}</p>
              <p className="mt-0.5 text-[10px] font-semibold uppercase tracking-wide text-teal-700">{selectedNode.type || "concept"}</p>
            </div>
            <button aria-label="Close concept details" className="rounded p-1 text-slate-400 hover:bg-slate-100" onClick={() => {
              setSelectedNode(null);
              cyRef.current?.elements().removeClass("selectedPath dimmed");
            }} type="button">
              <X className="h-4 w-4" />
            </button>
          </div>
          <p className="mt-2 text-xs leading-5 text-slate-600">{selectedNode.description || "No description was extracted for this concept."}</p>
          <div className="mt-3 rounded-md bg-slate-50 p-2.5 text-xs text-slate-600">
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
              className="mt-3 inline-flex items-center gap-2 rounded-md bg-teal-700 px-3 py-2 text-xs font-semibold text-white transition hover:bg-teal-800"
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
  );
}
