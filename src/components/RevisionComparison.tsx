import { useEffect, useState } from "react";
import { API_BASE_URL, fetchJson } from "../services/api";

type DocumentInfo = { id: string; filename: string; created_at: string };
type Pair = { earlier_id: string; later_id: string };
type Section = { id: string; page: number; heading: string; text: string };
type Review = { earlier_section: string; later_section: string | null; version: number; evidence_digest: string };
type Result = {
  earlier: DocumentInfo; later: DocumentInfo; version: number; evidence_digest: string;
  counts: Record<string, number>;
  changes: { kind: string; basis: string; earlier: Section | null; later: Section | null; diff: string[] }[];
  pending: { earlier: Section; suggested: string | null }[];
  candidates: Section[]; earlier_review_pages: number[]; later_review_pages: number[];
};
const control = "max-w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-white/20 dark:bg-slate-900";
const action = "rounded border border-indigo-300 px-3 py-1.5 text-sm font-semibold text-indigo-700 disabled:opacity-50 dark:text-indigo-300";
const docLabel = (doc: DocumentInfo) => `${doc.filename} · ${new Date(doc.created_at).toLocaleDateString()} · ${doc.id.slice(0, 8)}`;
const sectionLabel = (section: Section) => `${section.heading || "Untitled section"} · p. ${section.page} · § ${section.id}`;

function SourcePage({ doc, page }: { doc: DocumentInfo; page: number }) {
  return <a className="break-all text-indigo-700 underline dark:text-indigo-300" target="_blank" rel="noreferrer"
    href={`${API_BASE_URL}/ingest/uploads/${encodeURIComponent(doc.id)}/preview#page=${page}`}>{doc.filename} · p. {page}</a>;
}

function ReviewSection({ earlier, suggested, result, onSave, busy }: {
  earlier: Section; suggested: string | null; result: Result; onSave: (review: Review) => void; busy: boolean;
}) {
  const [choice, setChoice] = useState("");
  const candidate = result.candidates.find(item => item.id === choice);
  return <details className="rounded border border-amber-300 p-3">
    <summary className="cursor-pointer font-medium">{sectionLabel(earlier)}</summary>
    <p className="mt-2"><SourcePage doc={result.earlier} page={earlier.page} /></p>
    <pre className="my-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words text-xs">{earlier.text}</pre>
    <label className="block">Corresponding revised section
      <select className={`${control} mt-1 block w-full`} value={choice} onChange={e => setChoice(e.target.value)}>
        <option value="">Choose after checking the source</option>
        {result.candidates.map(item => <option key={item.id} value={item.id}>{sectionLabel(item)}{item.id === suggested ? " (closest wording)" : ""}</option>)}
        <option value="removed">No corresponding revised section</option>
      </select>
    </label>
    {candidate && <div className="mt-2"><SourcePage doc={result.later} page={candidate.page} />
      <pre className="my-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words text-xs">{candidate.text}</pre></div>}
    <button type="button" disabled={!choice || busy} className={`${action} mt-2`} onClick={() => onSave({
      earlier_section: earlier.id, later_section: choice === "removed" ? null : choice,
      version: result.version, evidence_digest: result.evidence_digest,
    })}>Save reviewed alignment</button>
  </details>;
}

export default function RevisionComparison({ courseId }: { courseId: string }) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<{ documents: DocumentInfo[]; pairs: Pair[] } | null>(null);
  const [earlier, setEarlier] = useState("");
  const [later, setLater] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const endpoint = `${API_BASE_URL}/courses/${encodeURIComponent(courseId)}/revisions`;

  useEffect(() => {
    if (!open || !courseId) return;
    let active = true;
    setOptions(null);
    setError("");
    fetchJson<{ documents: DocumentInfo[]; pairs: Pair[] }>(endpoint, { method: "GET" })
      .then(data => { if (active) setOptions(data); })
      .catch((e: Error) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [open, endpoint, courseId]);

  async function compare(review?: Review) {
    setBusy(true);
    setError("");
    try {
      const data = await fetchJson<Result>(`${endpoint}/compare`, {
        method: "POST", headers: { "Content-Type": "application/json" }, timeout: 90000,
        body: JSON.stringify({ earlier_id: earlier, later_id: later, review }),
      });
      setResult(data);
      setOptions(previous => {
        if (!previous || previous.pairs.some(p => p.earlier_id === earlier && p.later_id === later)) return previous;
        return { ...previous, pairs: [...previous.pairs, { earlier_id: earlier, later_id: later }] };
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Comparison failed.");
    } finally {
      setBusy(false);
    }
  }

  function selectPair(old: string, next: string) {
    setEarlier(old); setLater(next); setResult(null); setError("");
  }
  return <details className="rounded-md border border-slate-200 bg-white p-4 text-sm dark:border-white/10 dark:bg-[#0B0B0F]"
    onToggle={e => setOpen(e.currentTarget.open)}>
    <summary className="cursor-pointer font-semibold">Compare document revisions</summary>
    {open && <div className="mt-3 space-y-3">
      {!options && !error && <p role="status">Loading course documents…</p>}
      {options && options.documents.length < 2 && <p>Two distinct ready PDFs in this course are needed for comparison.</p>}
      {options && options.documents.length >= 2 && <>
        {options.pairs.length > 0 && <label className="block">Saved revision pairs
          <select className={`${control} mt-1 block w-full`} value="" disabled={busy} onChange={e => {
            const pair = options.pairs[Number(e.target.value)];
            if (pair) selectPair(pair.earlier_id, pair.later_id);
          }}><option value="">Choose a saved pair</option>
            {options.pairs.map((pair, index) => <option key={index} value={index}>
              {options.documents.find(d => d.id === pair.earlier_id)?.filename} ({pair.earlier_id.slice(0, 8)}) → {options.documents.find(d => d.id === pair.later_id)?.filename} ({pair.later_id.slice(0, 8)})
            </option>)}</select></label>}
        <div className="grid gap-3 sm:grid-cols-2">
          {(["Earlier PDF", "Revised PDF"] as const).map((label, i) => <label key={label}>{label}
            <select className={`${control} mt-1 block w-full`} value={i ? later : earlier} disabled={busy}
              onChange={e => i ? selectPair(earlier, e.target.value) : selectPair(e.target.value, later)}>
              <option value="">Choose an upload</option>
              {options.documents.map(doc => <option key={doc.id} value={doc.id} disabled={doc.id === (i ? earlier : later)}>{docLabel(doc)}</option>)}
            </select></label>)}
        </div>
        <button type="button" className={action} disabled={busy || !earlier || !later || earlier === later}
          onClick={() => void compare()}>{busy ? "Comparing…" : "Compare and save pair"}</button>
      </>}
      {error && <p role="alert" className="text-red-600">{error}</p>}
      {result && <div className="space-y-3" aria-live="polite">
        <h3 className="font-semibold">Changes in extracted PDF text</h3>
        <p>{Object.entries(result.counts).map(([kind, count]) => `${count} ${kind}`).join(" · ") || "No resolved text changes."}</p>
        {([["Earlier", result.earlier, result.earlier_review_pages], ["Revised", result.later, result.later_review_pages]] as const)
          .map(([label, doc, pages]) => pages.length > 0 && <p key={label} className="rounded bg-amber-50 p-2 text-amber-900">
            Check the {label.toLowerCase()} PDF for image content or unreliable text. {pages.map(page =>
              <span key={page} className="mr-2 inline-block"><SourcePage doc={doc} page={page} /></span>)}</p>)}
        {result.changes.map(change => {
          const before = change.diff.find(line => line.startsWith("-"))?.slice(1);
          const after = change.diff.find(line => line.startsWith("+"))?.slice(1);
          return <article key={`${change.earlier?.id}-${change.later?.id}`} className="space-y-1.5 rounded border border-slate-200 p-3 dark:border-white/10">
            <h4><strong className="capitalize">{change.kind}</strong> · {change.later?.heading || change.earlier?.heading || "Untitled section"}</h4>
            {change.earlier && <p>Earlier · <SourcePage doc={result.earlier} page={change.earlier.page} />
              {before && <q className="ml-2 break-words">{before}</q>}</p>}
            {change.later && <p>Revised · <SourcePage doc={result.later} page={change.later.page} />
              {after && <q className="ml-2 break-words">{after}</q>}</p>}
            {!!change.diff.length && <details><summary className="cursor-pointer text-xs font-semibold">Full text difference</summary>
              <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words bg-slate-100 p-2 text-xs dark:bg-slate-800">{change.diff.join("\n")}</pre></details>}
            {change.basis === "reviewed" && <p className="text-xs">Alignment confirmed by a reviewer.</p>}
          </article>;
        })}
        {result.pending.length > 0 && <><h3 className="font-semibold">Alignment review required</h3>
          <p>{result.pending.length} earlier section(s) have unresolved matches. Their changes are excluded from the summary until reviewed.</p>
          {result.pending.map(p => <ReviewSection key={`${result.version}-${p.earlier.id}`} earlier={p.earlier}
            suggested={p.suggested} result={result} onSave={review => void compare(review)} busy={busy} />)}</>}
      </div>}
    </div>}
  </details>;
}
