import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { BookOpen, LoaderCircle } from "lucide-react";

import type {
  GraphCanvasEdge,
  GraphCanvasNode,
} from "@/components/ConceptGraphCanvas";
import PdfPreviewModal from "@/components/PdfPreviewModal";
import SavedSampleCourse from "./SavedSampleCourse";
import {
  API_BASE_URL,
  getPublicSample,
  type GraphContextItem,
  type PublicSampleResponse,
} from "@/services/api";

const ConceptGraphCanvas = lazy(() => import("@/components/ConceptGraphCanvas"));

export default function PublicSampleCourse(): JSX.Element {
  const [showLive, setShowLive] = useState(false);
  return <div className="space-y-4">
    <SavedSampleCourse />
    <button type="button" aria-expanded={showLive} onClick={() => setShowLive(!showLive)}
      className="text-sm text-teal-700 underline underline-offset-4">
      {showLive ? "Hide live sample" : "Explore the live uploaded course (requires server connection)"}
    </button>
    {showLive ? <LiveSampleCourse /> : null}
  </div>;
}

function LiveSampleCourse(): JSX.Element {
  const [sample, setSample] = useState<PublicSampleResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ title: string; url: string } | null>(null);

  useEffect(() => {
    let active = true;
    getPublicSample()
      .then((result) => {
        if (active) setSample(result);
      })
      .catch(() => {
        if (active) setError("The public sample is temporarily unavailable.");
      });
    return () => {
      active = false;
    };
  }, []);

  const graph = useMemo(
    () => buildSampleGraph(sample?.graph_context ?? []),
    [sample],
  );

  return (
    <section className="min-w-0 overflow-hidden rounded-xl border border-slate-200 bg-white p-5 shadow-xl shadow-slate-200/40 dark:border-white/10 dark:bg-[#15151b] dark:shadow-black/30">
      <PdfPreviewModal
        isOpen={preview !== null}
        onClose={() => setPreview(null)}
        previewUrl={preview?.url ?? ""}
        title={preview?.title ?? "Public sample PDF"}
      />
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <span className="inline-flex items-center gap-1.5 rounded-full bg-teal-50 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-teal-700">
            <BookOpen className="h-3 w-3" />
            Public read-only sample
          </span>
          <h2 className="mt-2 text-lg font-semibold text-ink dark:text-white">
            {sample?.course_name ?? "Pre-uploaded sample course"}
          </h2>
          <p className="mt-1 text-sm leading-5 text-slate-500 dark:text-slate-400">
            Explore a prepared concept graph without an access code. Uploads and AI operations remain reviewer-only.
          </p>
        </div>
      </div>

      {!sample && !error ? (
        <div className="grid min-h-[360px] place-items-center rounded-lg border border-dashed border-slate-200 bg-slate-50 text-sm text-slate-500">
          <span className="inline-flex items-center gap-2">
            <LoaderCircle className="h-4 w-4 animate-spin" />
            Loading public sample...
          </span>
        </div>
      ) : null}
      {error ? (
        <div className="grid min-h-[240px] place-items-center rounded-lg border border-dashed border-slate-200 bg-slate-50 px-6 text-center text-sm text-slate-500">
          {error}
        </div>
      ) : null}
      {sample ? (
        <>
          <div className="mb-3 flex flex-wrap gap-2 text-xs text-slate-500">
            <span className="rounded-full bg-slate-100 px-2.5 py-1">
              {sample.documents.length} PDF{sample.documents.length === 1 ? "" : "s"}
            </span>
            <span className="rounded-full bg-slate-100 px-2.5 py-1">
              {sample.graph_metadata.total_nodes} concepts
            </span>
            <span className="rounded-full bg-slate-100 px-2.5 py-1">
              {sample.graph_status.split("_").join(" ").toLowerCase()}
            </span>
          </div>
          <div className="h-[430px]">
            <Suspense fallback={<div className="grid h-full place-items-center text-sm text-slate-500">Loading graph...</div>}>
              <ConceptGraphCanvas
                nodes={graph.nodes}
                edges={graph.edges}
                onOpenSource={(node) => {
                  if (!node.uploadId) return;
                  const pageSuffix = node.pageNumber ? `#page=${node.pageNumber}` : "";
                  setPreview({
                    title: node.pageNumber
                      ? `${node.documentName || "Sample PDF"} · Page ${node.pageNumber}`
                      : node.documentName || "Sample PDF",
                    url: `${API_BASE_URL}/public/sample/uploads/${node.uploadId}/preview${pageSuffix}`,
                  });
                }}
              />
            </Suspense>
          </div>
        </>
      ) : null}
    </section>
  );
}

function buildSampleGraph(graphContext: GraphContextItem[]): {
  nodes: GraphCanvasNode[];
  edges: GraphCanvasEdge[];
} {
  const nodes = new Map<string, GraphCanvasNode>();
  const edges = new Map<string, GraphCanvasEdge>();
  graphContext.forEach((item, itemIndex) => {
    const candidates = [item.concept, ...item.related_concepts];
    candidates.forEach((concept, conceptIndex) => {
      const id = concept.id ?? `sample-${itemIndex}-${conceptIndex}`;
      nodes.set(id, {
        id,
        label: concept.name ?? id,
        type: concept.type,
        description: concept.description,
        documentName: concept.document_name,
        pageNumber: concept.page_number,
        sectionHeading: concept.section_heading,
        uploadId: concept.upload_id,
      });
    });
  });
  graphContext.forEach((item) => {
    item.relationships.forEach((relationship) => {
      if (!nodes.has(relationship.source) || !nodes.has(relationship.target)) return;
      const id = `${relationship.source}->${relationship.target}:${relationship.type}`;
      edges.set(id, {
        id,
        source: relationship.source,
        target: relationship.target,
        label: relationship.type,
      });
    });
  });
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}
