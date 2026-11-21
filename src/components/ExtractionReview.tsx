import { useEffect, useState } from "react";
import { API_BASE_URL, fetchJson } from "../services/api";

type Block = { order: number; text: string; kind: string; bbox: [number, number, number, number] };
type Review = {
  page_number: number; page_count: number; width: number; height: number; image: string; blocks: Block[];
  extracted_text: string; uncertain_text: string; warnings: string[]; source_sha256: string;
  publication_enabled: boolean; reviewed_chunks: {id: string; text: string}[]; corrected_text: string | null; revision: number; task_id: string; can_correct: boolean;
};

export default function ExtractionReview({ uploadId, initialPage = 1, onQueued }: {
  uploadId: string; initialPage?: number; onQueued?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(initialPage);
  const [review, setReview] = useState<Review | null>(null);
  const [text, setText] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [highlight, setHighlight] = useState<Block | null>(null);
  const [refresh, setRefresh] = useState(0);
  const url = API_BASE_URL + "/ingest/uploads/" + encodeURIComponent(uploadId) + "/extraction/" + page;
  useEffect(() => {
    if (!open) return;
    let active = true;
    setReview(null);
    setMessage("");
    setHighlight(null);
    fetchJson<Review>(url, { timeout: 65000 }).then(data => {
      if (!active) return;
      setReview(data);
      setText(data.corrected_text ?? (data.extracted_text || data.uncertain_text));
    }).catch(error => { if (active) setMessage(error.message); });
    return () => { active = false; };
  }, [open, url, refresh]);

  async function save() {
    if (!review || busy) return;
    setBusy(true);
    try {
      const result = await fetchJson<{ status: string; rebuilt_chunks: number; revision: number }>(url, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, revision: review.revision, task_id: review.task_id, source_sha256: review.source_sha256 }) });
      setReview({ ...review, corrected_text: text, revision: result.revision, reviewed_chunks: [],
        can_correct: result.status !== "queued" });
      setMessage(result.status === "queued" ? "Correction saved. The updated document is queued for indexing."
        : "Correction saved. Rebuilt page chunks: " + result.rebuilt_chunks + ". Reload to inspect them.");
      if (result.status === "queued") onQueued?.();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not save the correction.");
    } finally { setBusy(false); }
  }

  return <div className="mt-2 text-xs text-slate-700">
    <button type="button" className="min-h-8 underline" aria-expanded={open} disabled={busy}
      onClick={() => setOpen(!open)}>{open ? "Close extraction review" : "Review extracted text"}</button>
    {open && <div className="space-y-3 rounded border border-slate-200 p-3">
      {message && <p role="status">{message} <button className="underline" disabled={busy}
        onClick={() => setRefresh(refresh + 1)}>Reload page</button></p>}
      {!review && !message && <p role="status">Loading original page and text…</p>}
      {review && <>
        <div className="flex items-center gap-3">
          <button type="button" disabled={busy || page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span>Page {page} of {review.page_count}</span>
          <button type="button" disabled={busy || page >= review.page_count} onClick={() => setPage(page + 1)}>Next</button>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <div className="relative">
              <img src={review.image} alt={"Original PDF page " + page} className="block w-full border" />
              {highlight && <span aria-hidden="true" className="pointer-events-none absolute border-2 border-amber-600 bg-amber-300/30"
                style={{ left: highlight.bbox[0]/review.width*100 + "%", top: highlight.bbox[1]/review.height*100 + "%",
                  width: (highlight.bbox[2]-highlight.bbox[0])/review.width*100 + "%",
                  height: (highlight.bbox[3]-highlight.bbox[1])/review.height*100 + "%" }} />}
            </div>
            {review.blocks.length > 0 && <details><summary className="cursor-pointer">Locate extracted lines</summary>
              <ol className="max-h-40 overflow-auto">{review.blocks.map(block =>
                <li key={block.order}><button type="button" className="text-left underline" onClick={() => setHighlight(block)}>
                  {block.order}. {block.text}</button></li>)}</ol></details>}
          </div>
          <div className="space-y-2">
            {review.warnings.length > 0 && <p className="text-amber-800">{review.warnings.map(w => w.replace(/_/g, " ")).join(", ")}</p>}
            {review.uncertain_text && <details open><summary>Unconfirmed OCR text</summary>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap">{review.uncertain_text}</pre></details>}
            <label className="block">Page transcription
              <textarea className="mt-1 w-full rounded border p-2" rows={10} value={text}
                maxLength={60000} disabled={busy || !review.can_correct} onChange={event => setText(event.target.value)} /></label>
            {review.reviewed_chunks.length > 0 && <details><summary>Inspect reviewed chunks ({review.reviewed_chunks.length})</summary>
              {review.reviewed_chunks.map(chunk => <p key={chunk.id} className="my-1 whitespace-pre-wrap">{chunk.text}</p>)}</details>}
            {review.can_correct && <><p>{review.publication_enabled
              ? "Saving reindexes this upload and can use the configured provider quota."
              : "Saving rebuilds this page’s review chunks. Search continues using the indexed transcription."}</p>
              <button type="button" disabled={busy || !text.trim() || text.trim() === (review.corrected_text ?? review.extracted_text).trim()}
                className="rounded border px-3 py-2 disabled:opacity-50" onClick={save}>
                {busy ? "Saving…" : review.publication_enabled ? "Save correction and reindex" : "Save page correction"}</button></>}
          </div>
        </div>
      </>}
    </div>}
  </div>;
}
