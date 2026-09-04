import { FormEvent, lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Check,
  ChevronDown,
  Copy,
  ExternalLink,
  Loader2,
  RefreshCw,
  RotateCcw,
  Send,
  Trash2,
  UploadCloud,
} from "lucide-react";

import type {
  GraphCanvasEdge,
  GraphCanvasNode,
} from "../components/ConceptGraphCanvas";
import PdfPreviewModal from "../components/PdfPreviewModal";
import UploadModal from "../components/UploadModal";
import {
  API_BASE_URL,
  CourseSummary,
  GraphContextItem,
  GraphStatus,
  IngestResponse,
  QueryResponse,
  UploadStatusResponse,
  getUploadStatus,
  listCourses,
  listUploads,
  retryUpload,
  removeUpload,
  sendQuery,
} from "../services/api";

const ConceptGraphCanvas = lazy(() => import("../components/ConceptGraphCanvas"));
const ExamPanel = lazy(() => import("../components/ExamPanel"));
const QUESTION_STARTERS = [
  "Summarize the most important concepts in this course.",
  "What should I learn first, and why?",
  "Explain the hardest concept with a simple example.",
];

type UploadJob = UploadStatusResponse & {
  status_poll_error?: string | null;
};

export default function Dashboard(): JSX.Element {
  const [question, setQuestion] = useState("");
  const [courseId, setCourseId] = useState("");
  const [response, setResponse] = useState<QueryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const [uploadJobs, setUploadJobs] = useState<UploadJob[]>([]);
  const [courses, setCourses] = useState<CourseSummary[]>([]);
  const [pendingCourseSelection, setPendingCourseSelection] = useState<string | null>(null);
  const [showAllUploads, setShowAllUploads] = useState(false);
  const [retryingUploadId, setRetryingUploadId] = useState<string | null>(null);
  const [deletingUploadId, setDeletingUploadId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<UploadJob | null>(null);
  const deleteCancelButtonRef = useRef<HTMLButtonElement>(null);
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [answerCopied, setAnswerCopied] = useState(false);
  const [queueFilter, setQueueFilter] = useState<"active" | "ready" | "failed" | "all">("all");
  const [selectedPreview, setSelectedPreview] = useState<{
    title: string;
    previewUrl: string;
  } | null>(null);

  const graphElements = useMemo(
    () => buildGraphElements(response?.graph_context ?? []),
    [response],
  );
  const selectedCourse = courses.find((course) => course.course_id === courseId);
  const canSubmitQuery =
    question.trim().length > 0 &&
    Boolean(selectedCourse && selectedCourse.ready_documents > 0);
  const filteredUploads = uploadJobs
    .filter((job) => queueFilter === "all" || job.status === queueFilter)
    .sort((left, right) => {
      const rank = { active: 0, ready: 1, failed: 2, cancelled: 3 };
      return rank[left.status] - rank[right.status] || Date.parse(right.updated_at) - Date.parse(left.updated_at);
    });
  const visibleUploads = showAllUploads ? filteredUploads : filteredUploads.slice(0, 4);
  const queueMetrics = uploadJobs.reduce(
    (totals, job) => ({
      active: totals.active + Number(job.status === "active"),
      ready: totals.ready + Number(job.status === "ready"),
      failed: totals.failed + Number(job.status === "failed"),
    }),
    { active: 0, ready: 0, failed: 0 },
  );

  async function refreshUploads(): Promise<void> {
    try {
      const [nextUploads, nextCourses] = await Promise.all([listUploads(), listCourses()]);
      setUploadJobs(nextUploads);
      setCourses(nextCourses);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Unable to load recent uploads.",
      );
    }
  }

  function selectCourse(nextCourseId: string): void {
    setCourseId(nextCourseId);
    setResponse(null);
    setError(null);
  }

  useEffect(() => {
    void refreshUploads();
  }, []);

  useEffect(() => {
    if (!deleteTarget) {
      deleteTriggerRef.current?.focus();
      deleteTriggerRef.current = null;
      return undefined;
    }
    deleteCancelButtonRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && deletingUploadId === null) {
        setDeleteTarget(null);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [deleteTarget, deletingUploadId]);

  useEffect(() => {
    const selectedCourseIsReady = courses.some(
      (course) => course.course_id === courseId && course.ready_documents > 0,
    );
    if (!selectedCourseIsReady) {
      const readyCourse = [...courses]
        .filter((course) => course.ready_documents > 0)
        .sort(
          (left, right) =>
            Date.parse(right.last_updated_at ?? "") -
            Date.parse(left.last_updated_at ?? ""),
        )[0];
      selectCourse(readyCourse?.course_id ?? "");
    }
  }, [courseId, courses]);

  useEffect(() => {
    const pending = uploadJobs.filter(
      (job) => job.status === "active",
    );
    if (pending.length === 0) {
      return undefined;
    }

    const intervalId = window.setInterval(async () => {
      const results = await Promise.allSettled(
        pending.map(async (job) => getUploadStatus(job.task_id)),
      );

      const terminalUpdates = results.flatMap((result) =>
        result.status === "fulfilled" && result.value.status !== "active"
          ? [result.value]
          : [],
      );

      setUploadJobs((current) =>
        current.map((job) => {
          const pendingIndex = pending.findIndex(
            (pendingJob) => pendingJob.task_id === job.task_id,
          );
          if (pendingIndex === -1) {
            return job;
          }

          const result = results[pendingIndex];
          if (!result) {
            return job;
          }

          if (result.status === "fulfilled") {
            return {
              ...result.value,
              status_poll_error: null,
            };
          }

          return {
            ...job,
            status_poll_error:
              result.reason instanceof Error
                ? result.reason.message
                : "Unable to refresh this upload status.",
          };
        }),
      );

      if (terminalUpdates.length > 0) {
        try {
          const nextCourses = await listCourses();
          setCourses(nextCourses);
          const preferredReadyCourse = pendingCourseSelection
            ? nextCourses.find(
                (course) =>
                  course.course_id === pendingCourseSelection &&
                  course.ready_documents > 0,
              )
            : undefined;
          if (preferredReadyCourse) {
            selectCourse(preferredReadyCourse.course_id);
            setPendingCourseSelection(null);
          } else if (
            pendingCourseSelection &&
            terminalUpdates.some(
              (update) =>
                update.course_id === pendingCourseSelection &&
                update.status === "failed",
            )
          ) {
            setPendingCourseSelection(null);
          }
        } catch (requestError) {
          setError(
            requestError instanceof Error
              ? requestError.message
              : "The document finished, but course selection could not be refreshed.",
          );
        }
      }
    }, 2500);

    return () => window.clearInterval(intervalId);
  }, [pendingCourseSelection, uploadJobs]);

  function handleUploadCreated(upload: IngestResponse): void {
    const now = new Date().toISOString();
    setUploadJobs((current) => [
      {
        upload_id: upload.upload_id,
        task_id: upload.task_id,
        course_id: upload.course_id,
        course_name: upload.course_name,
        original_filename: upload.original_filename,
        status: upload.status === "READY" ? "ready" : "active",
        stage: upload.status,
        failure_category: null,
        retryable: false,
        attempt_count: 1,
        last_attempted_at: now,
        processed_chunk_count: 0,
        graph_node_count: 0,
        graph_edge_count: 0,
        graph_status: null,
        error_message: null,
        result_json: null,
        created_at: now,
        updated_at: now,
        started_at: null,
        completed_at: null,
        preview_url: upload.preview_url,
      },
      ...current.filter((job) => job.upload_id !== upload.upload_id),
    ]);
    setPendingCourseSelection(upload.course_id);
    if (upload.status === "READY") {
      selectCourse(upload.course_id);
      setPendingCourseSelection(null);
    }
    void refreshUploads();
  }

  async function handleRetry(job: UploadJob): Promise<void> {
    setRetryingUploadId(job.upload_id);
    setError(null);
    try {
      const upload = await retryUpload(job.upload_id);
      handleUploadCreated(upload);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to retry upload.");
    } finally {
      setRetryingUploadId(null);
    }
  }

  async function handleRemove(job: UploadJob): Promise<void> {
    setDeletingUploadId(job.upload_id);
    setError(null);
    try {
      await removeUpload(job.upload_id);
      setUploadJobs((current) => current.filter((item) => item.upload_id !== job.upload_id));
      if (job.course_id === courseId) {
        setResponse(null);
      }
      setSelectedPreview(null);
      setDeleteTarget(null);
      await refreshUploads();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to delete document.");
    } finally {
      setDeletingUploadId(null);
    }
  }

  async function copyAnswer(): Promise<void> {
    if (!response?.answer) return;
    await navigator.clipboard.writeText(response.answer);
    setAnswerCopied(true);
    window.setTimeout(() => setAnswerCopied(false), 1500);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!canSubmitQuery) {
      return;
    }

    setIsLoading(true);
    setError(null);

    try {
      const result = await sendQuery(question.trim(), courseId.trim());
      setResponse(result);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Unable to resolve the query.",
      );
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <main className="mx-auto grid min-h-[calc(100vh-104px)] w-full max-w-[1800px] grid-cols-1 gap-4 bg-slate-50/60 p-4 lg:grid-cols-[minmax(360px,420px)_1fr] lg:p-6 xl:grid-cols-[minmax(380px,440px)_1fr]">
      <PdfPreviewModal
        isOpen={selectedPreview !== null}
        onClose={() => setSelectedPreview(null)}
        previewUrl={selectedPreview?.previewUrl ?? ""}
        title={selectedPreview?.title ?? "PDF Preview"}
      />
      <UploadModal
        isOpen={isUploadModalOpen}
        onClose={() => setIsUploadModalOpen(false)}
        onUploaded={handleUploadCreated}
      />
      {deleteTarget ? (
        <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/55 p-4 backdrop-blur-sm">
          <section
            aria-describedby="delete-document-description"
            aria-labelledby="delete-document-title"
            aria-modal="true"
            className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 shadow-2xl"
            role="dialog"
          >
            <div className="mb-4 grid h-10 w-10 place-items-center rounded-lg bg-red-50 text-red-600">
              <Trash2 className="h-5 w-5" />
            </div>
            <h2 className="text-lg font-semibold text-ink" id="delete-document-title">
              Delete this PDF?
            </h2>
            <p className="mt-2 break-words text-sm font-medium text-slate-700">
              {deleteTarget.original_filename}
            </p>
            <p className="mt-2 text-sm leading-6 text-slate-500" id="delete-document-description">
              This permanently removes its processing record, vector chunks, graph concepts,
              relationships, and stored PDF. This action cannot be undone.
            </p>
            <div className="mt-6 flex justify-end gap-2">
              <button
                className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
                disabled={deletingUploadId !== null}
                onClick={() => setDeleteTarget(null)}
                ref={deleteCancelButtonRef}
                type="button"
              >
                Cancel
              </button>
              <button
                className="inline-flex items-center gap-2 rounded-md bg-red-600 px-3 py-2 text-sm font-semibold text-white transition hover:bg-red-700 disabled:cursor-wait disabled:opacity-60"
                disabled={deletingUploadId !== null}
                onClick={() => void handleRemove(deleteTarget)}
                type="button"
              >
                {deletingUploadId === deleteTarget.upload_id ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Trash2 className="h-4 w-4" />
                )}
                Delete permanently
              </button>
            </div>
          </section>
        </div>
      ) : null}
      <section className="flex min-h-[calc(100vh-136px)] flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel">
        <form className="border-b border-slate-200 p-4" onSubmit={handleSubmit}>
          <div className="space-y-3">
            <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
              Course (all READY PDFs)
              <select
                className="mt-1 h-10 w-full rounded-md border border-slate-300 px-3 text-sm font-medium text-ink outline-none transition focus:border-signal focus:ring-2 focus:ring-teal-100"
                value={courseId}
                onChange={(event) => selectCourse(event.target.value)}
                disabled={courses.length === 0}
              >
                {courses.length === 0 ? <option value="">Upload a course PDF to begin</option> : null}
                {courses.map((course) => (
                  <option
                    disabled={course.ready_documents === 0}
                    key={course.course_id}
                    value={course.course_id}
                  >
                    {course.course_name} · {course.ready_documents} ready · {course.processed_chunk_count} chunks
                  </option>
                ))}
              </select>
            </label>
            <p className="text-xs text-slate-500">
              A new upload is selected automatically when processing reaches READY.
            </p>
            {selectedCourse ? (
              <div className="grid grid-cols-3 gap-2" aria-label="Selected course metrics">
                <Metric label="Ready PDFs" value={selectedCourse.ready_documents} />
                <Metric label="Chunks" value={selectedCourse.processed_chunk_count} />
                <Metric label="Extracted nodes / edges" value={`${selectedCourse.graph_node_count} / ${selectedCourse.graph_edge_count}`} />
              </div>
            ) : null}
            {selectedCourse?.duplicate_records ? (
              <p className="text-xs text-amber-700">
                {selectedCourse.duplicate_records} historical duplicate records are excluded from these course totals.
              </p>
            ) : null}
            <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
              Student Question
              <textarea
                className="mt-1 min-h-28 w-full resize-none rounded-md border border-slate-300 px-3 py-2 text-sm leading-6 text-ink outline-none transition focus:border-signal focus:ring-2 focus:ring-teal-100"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                placeholder="Ask anything grounded in your uploaded PDFs..."
              />
            </label>
            <div className="flex flex-wrap gap-2">
              {QUESTION_STARTERS.map((starter) => (
                <button
                  className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1.5 text-left text-xs text-slate-600 transition hover:border-teal-300 hover:bg-teal-50 hover:text-teal-800"
                  key={starter}
                  onClick={() => setQuestion(starter)}
                  type="button"
                >
                  {starter}
                </button>
              ))}
            </div>
            <button
              className="inline-flex h-10 w-full items-center justify-center gap-2 rounded-md bg-ink px-4 text-sm font-semibold text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-400"
              type="submit"
              disabled={isLoading || !canSubmitQuery}
            >
              {isLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : (
                <Send className="h-4 w-4" aria-hidden="true" />
              )}
              Query ConceptGraph
            </button>
          </div>
        </form>

        <div className="relative border-b border-slate-200 bg-gradient-to-b from-white to-slate-50/70 p-4">
          {isLoading ? (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/70 backdrop-blur-sm">
              <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 shadow-sm">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-teal-600" />
                Building a grounded answer…
              </span>
            </div>
          ) : null}

          <div className="mb-3 flex min-h-7 items-center justify-between gap-3">
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Grounded answer</p>
              {response?.answer ? (
                <span className={`mt-1 inline-flex rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide ${confidenceClass(response.confidence.level)}`}>
                  {response.confidence.level} confidence · {Math.round(response.confidence.score * 100)}%
                </span>
              ) : null}
            </div>
            {response?.answer ? (
              <button className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-600 shadow-sm hover:bg-slate-50" onClick={() => void copyAnswer()} type="button">
                {answerCopied ? <Check className="h-3.5 w-3.5 text-teal-600" /> : <Copy className="h-3.5 w-3.5" />}
                {answerCopied ? "Copied" : "Copy"}
              </button>
            ) : null}
          </div>

          {error ? (
            <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          ) : null}

          <article className="prose prose-slate max-h-[28rem] max-w-none overflow-y-auto break-words pr-1 text-sm prose-headings:mb-2 prose-headings:mt-5 prose-p:my-3 prose-p:leading-6 prose-li:my-1 dark:prose-invert">
            {response?.answer ? (
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  table: ({ node: _node, ...props }) => (
                    <div className="my-4 overflow-x-auto rounded-md border border-slate-200 dark:border-white/10">
                      <table className="m-0 min-w-[34rem] border-collapse text-left text-xs" {...props} />
                    </div>
                  ),
                  th: ({ node: _node, ...props }) => (
                    <th className="border-b border-slate-200 bg-slate-50 px-3 py-2 font-semibold text-slate-700 dark:border-white/10 dark:bg-white/5 dark:text-slate-200" {...props} />
                  ),
                  td: ({ node: _node, ...props }) => (
                    <td className="border-b border-slate-100 px-3 py-2 align-top leading-5 last:border-b-0 dark:border-white/5" {...props} />
                  ),
                }}
              >
                {response.answer}
              </ReactMarkdown>
            ) : (
              <p className="my-0 text-slate-500">
                Ask a course question to see a formatted answer, confidence score, and page-level evidence here.
              </p>
            )}
          </article>

          {response?.sources.length ? (
            <div className="mt-5 space-y-2">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                Evidence · {response.sources.length} passages
              </h2>
              {response.sources.map((source, index) => (
                <details className="rounded-md border border-slate-200 bg-white p-3" key={source.source_id}>
                  <summary className="cursor-pointer list-none text-xs font-semibold text-slate-600">
                    <span className="flex items-center justify-between gap-3">
                      <span className="min-w-0 truncate">{source.document_name} · {formatPage(source.page_number)}{source.section_heading ? ` · ${source.section_heading}` : ""}</span>
                      <span className="shrink-0 text-teal-700">View evidence</span>
                    </span>
                  </summary>
                  <button
                    className="mb-2 mt-3 inline-flex items-center gap-2 text-xs font-semibold text-teal-700 hover:text-teal-800"
                    onClick={() => {
                      const uploadId = typeof source.document_id === "string" ? source.document_id : "";
                      if (!uploadId) return;
                      const pageNumber = typeof source.page_number === "number" ? source.page_number : undefined;
                      const pageSuffix = typeof pageNumber === "number" ? `#page=${pageNumber}` : "";
                      const citationPath = source.preview_url || `/ingest/uploads/${uploadId}/preview${pageSuffix}`;
                      setSelectedPreview({
                        title: typeof pageNumber === "number" ? `Source ${index + 1} · Page ${pageNumber}` : `Source ${index + 1}`,
                        previewUrl: `${API_BASE_URL}${citationPath}`,
                      });
                    }}
                    type="button"
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                    Open cited PDF page
                  </button>
                  <p className="text-sm leading-6 text-slate-700">{source.supporting_passage}</p>
                </details>
              ))}
            </div>
          ) : null}
        </div>

        <Suspense fallback={<div className="border-b border-slate-200 p-4 text-xs text-slate-500">Loading practice tools...</div>}>
          <ExamPanel courseId={courseId} />
        </Suspense>

        <details className="group border-b border-slate-200 bg-slate-50/40">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-xs font-semibold uppercase tracking-wide text-slate-500 transition hover:bg-slate-50">
            <span className="inline-flex items-center gap-2">
              Documents & processing
              <ChevronDown className="h-3.5 w-3.5 transition group-open:rotate-180" />
            </span>
            <span className="normal-case tracking-normal text-slate-400">
              {queueMetrics.active} active · {queueMetrics.ready} ready · {queueMetrics.failed} failed
            </span>
          </summary>
          <div className="border-t border-slate-200 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Processing queue
            </h2>
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-400">{uploadJobs.length} documents</span>
              <button
                aria-label="Refresh uploads"
                className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                onClick={() => void refreshUploads()}
                type="button"
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
          <div className="mb-3 flex gap-1 rounded-md bg-slate-100 p-1">
            {(["active", "ready", "failed", "all"] as const).map((filter) => (
              <button className={`flex-1 rounded px-2 py-1 text-[10px] font-semibold uppercase tracking-wide ${queueFilter === filter ? "bg-white text-ink shadow-sm" : "text-slate-500"}`} key={filter} onClick={() => setQueueFilter(filter)} type="button">
                {filter} {filter === "all" ? uploadJobs.length : queueMetrics[filter]}
              </button>
            ))}
          </div>
          {uploadJobs.length ? (
            <div className="space-y-2">
              {visibleUploads.map((job) => (
                <div
                  className="rounded-md border border-slate-200 bg-panel px-3 py-2"
                  key={job.task_id}
                >
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-ink">
                        {job.original_filename}
                      </p>
                      <button
                        className="text-xs text-slate-500 hover:text-teal-700 disabled:cursor-default disabled:hover:text-slate-500"
                        disabled={!courses.some((course) => course.course_id === job.course_id && course.ready_documents > 0)}
                        onClick={() => selectCourse(job.course_id)}
                        type="button"
                      >
                        Course {job.course_name}
                      </button>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <span className={statusClass(job.status)}>
                        {job.status}
                      </span>
                      {job.status === "ready" ? (
                        <button
                          className="inline-flex items-center gap-1 rounded-md border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                          onClick={() =>
                            setSelectedPreview({
                              title: job.original_filename,
                              previewUrl: `${API_BASE_URL}/ingest/uploads/${job.upload_id}/preview`,
                            })
                          }
                          type="button"
                        >
                          <ExternalLink className="h-3.5 w-3.5" />
                          Preview
                        </button>
                      ) : null}
                      {job.status === "failed" && job.retryable ? (
                        <button
                          className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                          disabled={retryingUploadId === job.upload_id}
                          onClick={() => void handleRetry(job)}
                          type="button"
                        >
                          {retryingUploadId === job.upload_id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
                          Retry
                        </button>
                      ) : null}
                      {job.status === "ready" || job.status === "failed" ? (
                        <button
                          aria-label={`Delete ${job.original_filename}`}
                          className="grid h-7 w-7 place-items-center rounded-md border border-slate-200 bg-white text-slate-400 transition hover:border-red-200 hover:bg-red-50 hover:text-red-600"
                          onClick={(event) => {
                            deleteTriggerRef.current = event.currentTarget;
                            setDeleteTarget(job);
                          }}
                          title="Delete document and indexed data"
                          type="button"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                    </div>
                  </div>
                  {job.error_message || job.status_poll_error ? (
                    <p className="mt-2 text-xs text-red-600">
                      {friendlyUploadError(job.error_message ?? job.status_poll_error)}
                    </p>
                  ) : null}
                  <p className="mt-2 text-[10px] text-slate-400">
                    {job.stage.split("_").join(" ")} · Attempt {job.attempt_count} · {formatRelativeTime(job.updated_at)}
                    {job.failure_category ? ` · ${job.failure_category.split("_").join(" ")}` : ""}
                  </p>
                  {job.graph_status ? (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <span className={`inline-flex rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${graphStatusStyle(job.graph_status)}`}>
                        {graphStatusLabel(job.graph_status)}
                      </span>
                      {graphCoverageLabel(job.result_json) ? (
                        <span className="text-[10px] font-medium text-slate-500">
                          {graphCoverageLabel(job.result_json)}
                        </span>
                      ) : null}
                      {graphLimitLabel(job.result_json) ? (
                        <span className="text-[10px] font-medium text-amber-700">
                          {graphLimitLabel(job.result_json)}
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ))}
              {filteredUploads.length > 4 ? (
                <button
                  className="inline-flex w-full items-center justify-center gap-1 rounded-md py-2 text-xs font-semibold text-slate-500 hover:bg-slate-50 hover:text-slate-800"
                  onClick={() => setShowAllUploads((value) => !value)}
                  type="button"
                >
                  <ChevronDown className={`h-3.5 w-3.5 transition ${showAllUploads ? "rotate-180" : ""}`} />
                  {showAllUploads ? "Show recent only" : `Show ${filteredUploads.length - 4} more`}
                </button>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-slate-500">
              Upload a syllabus to see live processing progress here.
            </p>
          )}
          </div>
        </details>

      </section>

      <section className="min-h-[calc(100vh-136px)] overflow-hidden rounded-xl border border-slate-200 bg-white p-4 shadow-panel">
        <div className="mb-3 flex items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-base font-semibold text-ink">Concept map</h1>
              {selectedCourse ? (
                <span className="max-w-64 truncate rounded-full bg-teal-50 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-teal-700">
                  {selectedCourse.course_name}
                </span>
              ) : null}
              {selectedCourse?.graph_status ? (
                <span className={`rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide ${graphStatusStyle(selectedCourse.graph_status)}`}>
                  {graphStatusLabel(selectedCourse.graph_status)}
                </span>
              ) : null}
              {selectedCourse && selectedCourse.graph_sections_total > 0 ? (
                <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-600">
                  {selectedCourse.graph_sections_succeeded} of {selectedCourse.graph_sections_total} sections represented
                </span>
              ) : null}
            </div>
            <p className="text-sm text-slate-500">
              {response?.graph_metadata
                ? `Showing ${response.graph_metadata.displayed_nodes} of ${response.graph_metadata.total_nodes} concepts and ${response.graph_metadata.displayed_edges} of ${response.graph_metadata.total_edges} relationships.`
                : "Ask a question to retrieve the most relevant concepts and prerequisite links."}
            </p>
            {response?.graph_metadata.graph_expansion?.anchor_match_found ? (
              <div className="mt-1 flex flex-wrap items-center gap-3 text-[11px] font-medium text-slate-500" aria-label="Graph retrieval legend">
                <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full border-2 border-teal-700 bg-cyan-50" />Query match</span>
                <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full border-2 border-emerald-500 bg-emerald-50" />1-hop ({response.graph_metadata.graph_expansion.one_hop_count})</span>
                <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full border-2 border-blue-400 bg-blue-50" />2-hop ({response.graph_metadata.graph_expansion.two_hop_count})</span>
              </div>
            ) : null}
          </div>
          <button
            onClick={() => setIsUploadModalOpen(true)}
            className="inline-flex shrink-0 items-center gap-2 rounded-md bg-teal-50 px-3 py-1.5 text-sm font-medium text-teal-700 transition hover:bg-teal-100 dark:bg-teal-900/30 dark:text-teal-300 dark:hover:bg-teal-900/50"
          >
            <UploadCloud className="h-4 w-4" />
            Add PDF
          </button>
        </div>
        <div className="relative h-[calc(100%-56px)]">
          {isLoading && (
            <div className="absolute inset-0 z-10 flex items-center justify-center rounded-md bg-white/60 backdrop-blur-sm dark:bg-[#0B0B0F]/60">
              <Loader2 className="h-8 w-8 animate-spin text-teal-500" />
            </div>
          )}
          {graphElements.nodes.length > 0 ? (
            <Suspense fallback={<div className="grid h-full min-h-[480px] place-items-center rounded-md border border-slate-200 bg-panel text-sm text-slate-500">Loading concept map...</div>}>
              <ConceptGraphCanvas
                nodes={graphElements.nodes}
                edges={graphElements.edges}
                onOpenSource={(node) => {
                  if (!node.uploadId) return;
                  const pageSuffix = node.pageNumber ? `#page=${node.pageNumber}` : "";
                  setSelectedPreview({
                    title: node.pageNumber
                      ? `${node.documentName || "Source PDF"} · Page ${node.pageNumber}`
                      : node.documentName || "Source PDF",
                    previewUrl: `${API_BASE_URL}/ingest/uploads/${node.uploadId}/preview${pageSuffix}`,
                  });
                }}
              />
            </Suspense>
          ) : (
            <div className="grid h-full min-h-[480px] place-items-center rounded-lg border border-dashed border-slate-300 bg-[radial-gradient(circle_at_center,_rgba(13,148,136,0.06),_transparent_55%)] px-8 text-center">
              <div className="max-w-sm">
                <div className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-xl bg-teal-50 text-lg font-bold text-teal-700">CG</div>
                <p className="font-semibold text-ink">
                  {selectedCourse?.graph_status === "READY_WITHOUT_GRAPH"
                    ? "No validated graph was produced"
                    : "Your course graph will appear here"}
                </p>
                <p className="mt-1 text-sm leading-6 text-slate-500">
                  {selectedCourse?.graph_status === "READY_WITHOUT_GRAPH"
                    ? "The PDF is still searchable for grounded answers, but the extracted graph did not meet the minimum quality checks."
                    : "Select a ready course and ask a question to reveal relevant concepts, relationships, and prerequisites."}
                </p>
              </div>
            </div>
          )}
        </div>
      </section>
    </main>
  );
}

function buildGraphElements(graphContext: GraphContextItem[]): {
  nodes: GraphCanvasNode[];
  edges: GraphCanvasEdge[];
} {
  const nodes = new Map<string, GraphCanvasNode>();
  const edges = new Map<string, GraphCanvasEdge>();

  const upsertNode = (node: GraphCanvasNode): void => {
    const existing = nodes.get(node.id);
    if (!existing) {
      nodes.set(node.id, node);
      return;
    }
    const hops = [existing.retrievalHop, node.retrievalHop].filter(
      (hop): hop is 0 | 1 | 2 => hop !== undefined,
    );
    nodes.set(node.id, {
      ...existing,
      ...node,
      retrievalHop: hops.length > 0 ? Math.min(...hops) as 0 | 1 | 2 : undefined,
    });
  };

  graphContext.forEach((item, itemIndex) => {
    const conceptId = item.concept.id ?? `concept-${itemIndex}`;
    upsertNode({
      id: conceptId,
      label: item.concept.name ?? conceptId,
      type: item.concept.type,
      description: item.concept.description,
      documentName: item.concept.document_name,
      pageNumber: item.concept.page_number,
      sectionHeading: item.concept.section_heading,
      uploadId: item.concept.upload_id,
      retrievalHop: item.concept.retrieval_hop,
    });

    (item.related_concepts ?? item.prerequisites).forEach((relatedConcept, relatedIndex) => {
      const relatedId =
        relatedConcept.id ?? `${conceptId}-related-${relatedIndex}`;
      upsertNode({
        id: relatedId,
        label: relatedConcept.name ?? relatedId,
        type: relatedConcept.type,
        description: relatedConcept.description,
        documentName: relatedConcept.document_name,
        pageNumber: relatedConcept.page_number,
        sectionHeading: relatedConcept.section_heading,
        uploadId: relatedConcept.upload_id,
        retrievalHop: relatedConcept.retrieval_hop,
      });

    });

    item.relationships.forEach((relationship) => {
      if (!nodes.has(relationship.source) || !nodes.has(relationship.target)) return;
      const edgeId = `${relationship.source}->${relationship.target}:${relationship.type}`;
      const sourceHop = nodes.get(relationship.source)?.retrievalHop;
      const targetHop = nodes.get(relationship.target)?.retrievalHop;
      const edgeHop = relationship.type === "PREREQUISITE_OF"
        ? Math.max(sourceHop ?? 0, targetHop ?? 0)
        : 0;
      edges.set(edgeId, {
        id: edgeId,
        source: relationship.source,
        target: relationship.target,
        label: relationship.type,
        retrievalHop: edgeHop === 1 || edgeHop === 2 ? edgeHop : undefined,
      });
    });
  });

  return {
    nodes: Array.from(nodes.values()),
    edges: Array.from(edges.values()),
  };
}

function formatRelativeTime(value: string): string {
  const elapsed = Date.now() - new Date(value).getTime();
  const minutes = Math.max(0, Math.floor(elapsed / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function formatPage(page: unknown): string {
  return typeof page === "number" ? `Page ${page}` : "PDF passage";
}

function graphStatusLabel(status: GraphStatus): string {
  if (status === "GRAPH_READY") return "Graph ready";
  if (status === "GRAPH_PARTIAL") return "Graph partial";
  return "Ready without graph";
}

function graphStatusStyle(status: GraphStatus): string {
  if (status === "GRAPH_READY") return "bg-emerald-50 text-emerald-700";
  if (status === "GRAPH_PARTIAL") return "bg-amber-50 text-amber-700";
  return "bg-slate-100 text-slate-600";
}

function graphCoverageLabel(resultJson: Record<string, unknown> | null | undefined): string | null {
  if (!resultJson) return null;
  const represented = resultJson.graph_sections_succeeded;
  const total = resultJson.graph_sections_total;
  if (
    typeof represented !== "number" ||
    typeof total !== "number" ||
    total <= 0
  ) {
    return null;
  }
  return `${represented} of ${total} sections represented`;
}

function graphLimitLabel(resultJson: Record<string, unknown> | null | undefined): string | null {
  if (!resultJson) return null;
  if (resultJson.graph_provider_limited === true) {
    return "Provider quota reached; completed graph data was preserved";
  }
  if (resultJson.graph_extraction_budget_applied === true) {
    return "Free-demo extraction budget applied";
  }
  return null;
}

function statusClass(status: UploadStatusResponse["status"]): string {
  const tone = {
    active: "bg-blue-50 text-blue-700",
    ready: "bg-teal-50 text-teal-700",
    failed: "bg-red-50 text-red-700",
    cancelled: "bg-slate-100 text-slate-600",
  }[status];
  return `rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide ${tone}`;
}

function confidenceClass(level: QueryResponse["confidence"]["level"]): string {
  return {
    high: "bg-teal-50 text-teal-700",
    medium: "bg-blue-50 text-blue-700",
    low: "bg-amber-50 text-amber-700",
    insufficient: "bg-red-50 text-red-700",
  }[level];
}

function friendlyUploadError(message: string | null | undefined): string {
  if (!message) return "Document processing failed. Please retry.";
  const normalized = message.toLowerCase();
  if (normalized.includes("different loop") || normalized.includes("future pending")) {
    return "The processing worker restarted unexpectedly. This issue is fixed; retry the document.";
  }
  if (normalized.includes("413") || normalized.includes("tokens per minute") || normalized.includes("rate_limit")) {
    return "The AI service could not process this request at the time. Retry with the optimized pipeline.";
  }
  if (normalized.includes("api key") || normalized.includes("unauthorized")) {
    return "The AI provider was not configured when this upload ran. Retry it now.";
  }
  return message.length > 180 ? "Document processing failed. Please retry it or upload another PDF." : message;
}

function Metric({ label, value }: { label: string; value: string | number }): JSX.Element {
  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</p>
      <p className="mt-0.5 text-sm font-semibold text-ink">{value}</p>
    </div>
  );
}
