import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import PdfPreviewModal from "./PdfPreviewModal";
import type { GraphCanvasEdge, GraphCanvasNode } from "./ConceptGraphCanvas";

const ConceptGraphCanvas = lazy(() => import("./ConceptGraphCanvas"));
const SAMPLES = [
  { id: "computing", title: "Computing Foundations", manifest: "course.json" },
  { id: "technical", title: "Equipment Documentation", manifest: "course-technical.json" },
] as const;
type SampleId = typeof SAMPLES[number]["id"];
type SourceReference = [string, number, number?];
interface Example {
  question: string;
  answer: string;
  sources: SourceReference[];
  concepts: string[];
  should_refuse?: boolean;
  nodes?: { id: string; label: string; description: string; source: SourceReference }[];
  relationships?: { source: string; target: string; type: string }[];
}
interface Course {
  saved_examples: Example[];
  description?: string;
  provenance?: string;
  documents: { filename: string; revision?: string; pages: { title: string; text: string; extraction_method?: string }[] }[];
}

export default function SavedSampleCourse() {
  const [sampleId, setSampleId] = useState<SampleId>(() =>
    new URLSearchParams(window.location.search).get("course") === "technical" ? "technical" : "computing");
  const [course, setCourse] = useState<Course | null>(null);
  const [selected, setSelected] = useState(0);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const [preview, setPreview] = useState<{ url: string; title: string } | null>(null);
  const sample = SAMPLES.find((item) => item.id === sampleId) ?? SAMPLES[0];
  useEffect(() => {
    const controller = new AbortController();
    setError(false);
    fetch(`/sample/${sample.manifest}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Sample unavailable");
        return response.json() as Promise<Course>;
      })
      .then((result) => { if (!controller.signal.aborted) setCourse(result); })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [sample.manifest, reload]);
  const example = course?.saved_examples[selected];
  const graph = useMemo(() => {
    if (!example) return { nodes: [], edges: [] };
    const authored = example.nodes ?? example.concepts.flatMap((name, index) => {
      const source = example.sources[index];
      return source ? [{ id: name, label: name, description: `Study ${name} at this point in the lesson sequence.`, source }] : [];
    });
    const nodes: GraphCanvasNode[] = authored.map((node) => ({
      id: node.id, label: node.label, description: node.description,
      documentName: node.source[0], pageNumber: node.source[1], uploadId: node.id,
      sectionHeading: course?.documents.find((doc) => doc.filename === node.source[0])?.pages[node.source[1] - 1]?.title,
    }));
    const relationships = example.relationships ?? example.concepts.slice(1).map((name, index) => ({
      source: example.concepts[index], target: name, type: "PREREQUISITE_OF",
    }));
    const edges: GraphCanvasEdge[] = relationships.map((edge, index) => ({
      id: `${edge.source}-${edge.target}-${index}`, source: edge.source, target: edge.target, relationType: edge.type,
    }));
    return { nodes, edges };
  }, [example, course]);
  const chooseSample = (id: SampleId) => {
    setSampleId(id);
    setCourse(null);
    setSelected(0);
    setPreview(null);
    setError(false);
    const url = new URL(window.location.href);
    if (id === "technical") url.searchParams.set("course", id);
    else url.searchParams.delete("course");
    window.history.replaceState(window.history.state, "", url);
  };
  const openSource = (filename: string, page: number) => setPreview({
    url: `/sample/${encodeURIComponent(filename)}#page=${page}`,
    title: `${filename} · Page ${page}`,
  });
  return (
    <section className="min-w-0 overflow-hidden rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6 dark:border-white/10 dark:bg-[#15151b]">
      <span className="text-xs font-semibold uppercase tracking-wide text-teal-700">Explore without a login</span>
      <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-900 dark:text-white">Explore {sample.title}</h1>
      <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">Three source PDFs, saved answers, and clickable source pages. These editorial examples and illustrated graphs are prepared in advance; selecting one makes no AI request.</p>
      <div className="mt-4 max-w-sm">
        <label htmlFor="sample-course" className="block text-sm font-medium text-slate-700 dark:text-slate-200">Sample course</label>
        <select id="sample-course" value={sampleId} onChange={(event) => chooseSample(event.target.value as SampleId)}
          className="mt-1 min-h-11 w-full rounded-lg border border-slate-300 bg-white px-3 text-sm text-slate-900 focus-visible:outline-teal-600 dark:border-white/20 dark:bg-[#15151b] dark:text-white">
          {SAMPLES.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
        </select>
      </div>
      {course?.description ? <p className="mt-3 max-w-prose text-sm leading-6 text-slate-600 dark:text-slate-300">{course.description} All equipment values are fictional, not operating advice.</p> : null}
      {error ? <div role="alert" className="mt-4 text-sm text-red-700">The saved sample could not load. <button type="button" className="min-h-11 underline underline-offset-4" onClick={() => setReload((value) => value + 1)}>Try again</button></div> : null}
      {!course && !error ? <p role="status" className="mt-4 text-sm text-slate-500">Loading saved examples…</p> : null}
      {course ? <details className="mt-4 text-sm text-slate-600 dark:text-slate-300">
        <summary className="min-h-8 cursor-pointer font-medium">Source PDFs — download and try in the reviewer workspace</summary>
        <ul className="mt-2 space-y-2">
          {course.documents.map((doc) => <li key={doc.filename} className="min-w-0">
            <a href={`/sample/${encodeURIComponent(doc.filename)}`} download className="inline-block min-h-8 break-words text-teal-800 underline underline-offset-4 dark:text-teal-200">{doc.filename}</a>
            <span className="ml-2 text-xs">{doc.pages.length} pages{doc.revision ? ` · ${doc.revision}` : ""}{doc.pages.some((page) => page.extraction_method === "ocr") ? " · Includes an image-only page" : ""}</span>
          </li>)}
        </ul>
        <p className="mt-2 max-w-prose text-xs leading-5">Downloads are public fixtures. Uploading them requires a reviewer code and uses the shared workspace and live provider quota. Saved examples are not retrieval measurements.</p>
      </details> : null}
      <div className="my-5 grid gap-2 sm:grid-cols-2 lg:grid-cols-3" aria-label="Saved sample questions">
        {course?.saved_examples.map((item, index) => (
          <button key={item.question} type="button" aria-pressed={index === selected}
            onClick={() => { setSelected(index); setPreview(null); }}
            className={`rounded-lg border px-3 py-2 text-left text-sm ${index === selected ? "border-teal-600 bg-teal-50 text-teal-900" : "border-slate-200 text-slate-600 hover:bg-slate-50"}`}>
            {item.question}
          </button>
        ))}
      </div>
      {example ? (
        <div className="grid min-w-0 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]" aria-live="polite">
          <div className="min-w-0 rounded-xl bg-slate-50 p-4 dark:bg-white/5">
          <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-white">{example.question}</h2>
          <p className="text-xs font-semibold uppercase text-teal-700 dark:text-teal-200">{example.should_refuse ? "Saved refusal example" : "Saved editorial answer"}</p>
          <p className="mt-2 text-sm leading-7 text-slate-700 dark:text-slate-200">{example.answer}</p>
          <div className="mt-3 grid gap-2">
            {example.sources.map(([filename, page, paragraph]) => {
              const source = course?.documents.find((doc) => doc.filename === filename)?.pages[page - 1];
              const passage = paragraph ? source?.text.split(/\n\s*\n/)[paragraph - 1] : undefined;
              return <div key={`${filename}:${page}:${paragraph ?? ""}`} className="min-w-0 border-t border-slate-200 pt-2 dark:border-white/10">
                {passage ? <p className="mb-2 text-xs leading-6 text-slate-600 dark:text-slate-300">{passage}</p> : null}
                <button type="button" onClick={() => openSource(filename, page)}
                  className="min-h-11 break-words rounded-md border border-slate-200 bg-white px-3 py-2 text-left text-xs text-teal-800 hover:bg-teal-50 focus-visible:outline-teal-600 dark:border-white/20 dark:bg-[#15151b] dark:text-teal-200 dark:hover:bg-white/5">
                  Open source: {filename} · Page {page}{paragraph ? ` · Paragraph ${paragraph}` : ""}
                </button>
                {source?.extraction_method === "ocr" ? <p className="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">Image-only page. Passage is the authored reference text; OCR is checked separately by the local verification script.</p> : null}
              </div>;
            })}
          </div>
          </div>
          <div className="min-w-0">
          <h2 className="text-sm font-semibold text-slate-900 dark:text-white">How the concepts connect</h2>
          <p className="mb-3 mt-1 text-xs leading-5 text-slate-500">Prepared illustration from the source lessons. Select a concept, then open its source page.</p>
          {example.should_refuse ? <div className="flex min-h-48 items-center rounded-lg border border-dashed border-slate-300 p-6 text-sm leading-6 text-slate-600 dark:border-white/20 dark:text-slate-300">No graph is drawn for an unsupported motor-power answer. The cited pages show why that fact cannot be established.</div> : <div className="h-[400px] min-w-0">
            <Suspense fallback={<p className="p-6 text-sm text-slate-500">Loading illustrated graph…</p>}>
              <ConceptGraphCanvas
                key={`${sampleId}:${example.question}`}
                layoutMode="breadthfirst"
                nodes={graph.nodes}
                edges={graph.edges}
                onOpenSource={(node) => {
                  if (node.documentName && node.pageNumber) openSource(node.documentName, node.pageNumber);
                }}
              />
            </Suspense>
          </div>}
          </div>
        </div>
      ) : null}
      <PdfPreviewModal isOpen={preview !== null} onClose={() => setPreview(null)} previewUrl={preview?.url ?? ""} title={preview?.title ?? "Source PDF"} />
    </section>
  );
}
