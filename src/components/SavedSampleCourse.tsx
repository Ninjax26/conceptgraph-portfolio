import { lazy, Suspense, useEffect, useState } from "react";
import PdfPreviewModal from "./PdfPreviewModal";

const ConceptGraphCanvas = lazy(() => import("./ConceptGraphCanvas"));
interface Example {
  question: string;
  answer: string;
  sources: [string, number][];
  concepts: string[];
}
interface Course {
  saved_examples: Example[];
  documents: { filename: string; pages: { title: string; text: string }[] }[];
}

export default function SavedSampleCourse() {
  const [course, setCourse] = useState<Course | null>(null);
  const [selected, setSelected] = useState(0);
  const [error, setError] = useState(false);
  const [preview, setPreview] = useState<{ url: string; title: string } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/sample/course.json", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Sample unavailable");
        return response.json() as Promise<Course>;
      })
      .then(setCourse)
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) setError(true);
      });
    return () => controller.abort();
  }, []);
  const example = course?.saved_examples[selected];
  const openSource = (filename: string, page: number) => setPreview({
    url: `/sample/${encodeURIComponent(filename)}#page=${page}`,
    title: `${filename} · Page ${page}`,
  });
  return (
    <section className="min-w-0 overflow-hidden rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6 dark:border-white/10 dark:bg-[#15151b]">
      <span className="text-xs font-semibold uppercase tracking-wide text-teal-700">Explore without a login</span>
      <h2 className="mt-2 text-2xl font-semibold tracking-tight text-slate-900 dark:text-white">Explore Computing Foundations</h2>
      <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">Three source PDFs, saved answers, and clickable source pages. These editorial examples and illustrated graphs are prepared in advance; selecting one makes no AI request.</p>
      {error ? <p role="alert" className="mt-4 text-sm text-red-700">The saved sample could not load. Refresh to try again.</p> : null}
      {!course && !error ? <p role="status" className="mt-4 text-sm text-slate-500">Loading saved examples…</p> : null}
      <div className="my-5 grid gap-2 sm:grid-cols-3" aria-label="Saved sample questions">
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
          <h3 className="mb-4 text-lg font-semibold text-slate-900 dark:text-white">{example.question}</h3>
          <p className="text-xs font-semibold uppercase text-teal-700">Saved editorial answer</p>
          <p className="mt-2 text-sm leading-7 text-slate-700 dark:text-slate-200">{example.answer}</p>
          <div className="mt-3 grid gap-2">
            {example.sources.map(([filename, page]) => (
              <button key={`${filename}:${page}`} type="button" onClick={() => openSource(filename, page)}
                className="break-words rounded-md border border-slate-200 bg-white px-3 py-3 text-left text-xs text-teal-800 hover:bg-teal-50 focus-visible:outline-teal-600">
                Open source: {filename} · Page {page}
              </button>
            ))}
          </div>
          </div>
          <div className="min-w-0">
          <h3 className="text-sm font-semibold text-slate-900 dark:text-white">How the concepts connect</h3>
          <p className="mb-3 mt-1 text-xs leading-5 text-slate-500">Prepared illustration from the source lessons. Select a concept, then open its source page.</p>
          <div className="h-[400px] min-w-0">
            <Suspense fallback={<p className="p-6 text-sm text-slate-500">Loading illustrated graph…</p>}>
              <ConceptGraphCanvas
                key={example.question}
                nodes={example.concepts.map((name, index) => ({
                  id: name, label: name, description: `Study ${name} at this point in the lesson sequence.`,
                  documentName: example.sources[index][0], pageNumber: example.sources[index][1], uploadId: name,
                }))}
                edges={example.concepts.slice(1).map((name, index) => ({
                  id: `${example.concepts[index]}-${name}`, source: example.concepts[index], target: name, label: "PREREQUISITE_OF",
                }))}
                onOpenSource={(node) => {
                  if (node.documentName && node.pageNumber) openSource(node.documentName, node.pageNumber);
                }}
              />
            </Suspense>
          </div>
          </div>
        </div>
      ) : null}
      <PdfPreviewModal isOpen={preview !== null} onClose={() => setPreview(null)} previewUrl={preview?.url ?? ""} title={preview?.title ?? "Source PDF"} />
    </section>
  );
}
