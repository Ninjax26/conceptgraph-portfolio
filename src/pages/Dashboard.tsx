import { FormEvent, lazy, Suspense, useEffect, useMemo, useState } from "react";
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
  IngestResponse,
  QueryResponse,
  UploadStatusResponse,
  getUploadStatus,
  listCourses,
  listUploads,
  retryUpload,
  removeFailedUpload,
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
    setError(null);
    try {
      await removeFailedUpload(job.upload_id);
      setUploadJobs((current) => current.filter((item) => item.upload_id !== job.upload_id));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to remove record.");
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
    <main className="grid min-h-[calc(100vh-64px)] grid-cols-1 gap-4 p-4 lg:grid-cols-[minmax(360px,440px)_1fr] lg:p-6">
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
      <section className="flex min-h-[calc(100vh-96px)] flex-col rounded-md border border-slate-200 bg-white shadow-panel">
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

        <Suspense fallback={<div className="border-b border-slate-200 p-4 text-xs text-slate-500">Loading practice tools...</div>}>
          <ExamPanel courseId={courseId} />
        </Suspense>

        <div className="border-b border-slate-200 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Processing Queue
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
                      {job.status === "failed" ? (
                        <button aria-label={`Remove ${job.original_filename}`} className="grid h-7 w-7 place-items-center rounded-md border border-slate-200 bg-white text-slate-400 hover:bg-red-50 hover:text-red-600" onClick={() => void handleRemove(job)} type="button">
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

        <div className="relative flex-1 overflow-y-auto p-4">
          {isLoading && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/60 backdrop-blur-sm dark:bg-[#0B0B0F]/60">
              <Loader2 className="h-8 w-8 animate-spin text-teal-500" />
            </div>
          )}

          {error ? (
            <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </div>
          ) : null}

          {response?.answer ? (
            <div className="mb-2 flex items-center justify-between gap-3">
              <span className={`rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide ${confidenceClass(response.confidence.level)}`}>
                {response.confidence.level} confidence · {Math.round(response.confidence.score * 100)}%
              </span>
              <button className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 px-2.5 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50" onClick={() => void copyAnswer()} type="button">
                {answerCopied ? <Check className="h-3.5 w-3.5 text-teal-600" /> : <Copy className="h-3.5 w-3.5" />}
                {answerCopied ? "Copied" : "Copy answer"}
              </button>
            </div>
          ) : null}
          <article className="prose prose-slate max-w-none break-words text-sm prose-headings:mb-2 prose-headings:mt-5 prose-p:my-3 prose-p:leading-6 prose-li:my-1 dark:prose-invert">
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
              <p className="text-slate-500">
                The answer and syllabus citations will appear here after a query.
              </p>
            )}
          </article>

          {response?.sources.length ? (
            <div className="mt-6 space-y-3">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                Source Citations
              </h2>
              {response.sources.map((source, index) => (
                <details
                  className="rounded-md border border-slate-200 bg-panel p-3"
                    key={source.source_id}
                >
                  <summary className="cursor-pointer list-none text-xs font-semibold text-slate-600">
                    <span className="flex items-center justify-between gap-3">
                      <span>{source.document_name} · {formatPage(source.page_number)}{source.section_heading ? ` · ${source.section_heading}` : ""}</span>
                      <span className="text-slate-400">View passage</span>
                    </span>
                  </summary>
                  <button
                    className="mb-2 inline-flex items-center gap-2 text-xs font-semibold text-teal-700 hover:text-teal-800"
                    onClick={() => {
                      const uploadId =
                        typeof source.document_id === "string"
                          ? source.document_id
                          : "";
                      if (!uploadId) {
                        return;
                      }

                      const pageNumber =
                        typeof source.page_number === "number"
                          ? source.page_number
                          : undefined;
                      const pageSuffix =
                        typeof pageNumber === "number" ? `#page=${pageNumber}` : "";
                      setSelectedPreview({
                        title:
                          typeof pageNumber === "number"
                            ? `Chunk ${index + 1} · Page ${pageNumber}`
                            : `Chunk ${index + 1}`,
                        previewUrl: `${API_BASE_URL}/ingest/uploads/${uploadId}/preview${pageSuffix}`,
                      });
                    }}
                    type="button"
                  >
                    <ExternalLink className="h-3.5 w-3.5" />
                    Open PDF preview
                  </button>
                  <p className="mt-2 text-sm leading-6 text-slate-700">
                    {source.supporting_passage}
                  </p>
                  <p className="mt-2 text-xs text-slate-500">
                    {typeof source.page_number === "number"
                      ? `Page ${source.page_number}`
                      : "Page unavailable"}
                  </p>
                </details>
              ))}
            </div>
          ) : null}
        </div>
      </section>

      <section className="min-h-[calc(100vh-96px)] rounded-md border border-slate-200 bg-white p-4 shadow-panel">
        <div className="mb-3 flex items-center justify-between gap-4">
          <div>
            <h1 className="text-base font-semibold text-ink">Concept Map</h1>
            <p className="text-sm text-slate-500">
              {response?.graph_metadata
                ? `Showing ${response.graph_metadata.displayed_nodes} of ${response.graph_metadata.total_nodes} concepts and ${response.graph_metadata.displayed_edges} of ${response.graph_metadata.total_edges} relationships.`
                : `${graphElements.nodes.length} concepts, ${graphElements.edges.length} relationships`}
            </p>
          </div>
          <button
            onClick={() => setIsUploadModalOpen(true)}
            className="inline-flex items-center gap-2 rounded-md bg-teal-50 px-3 py-1.5 text-sm font-medium text-teal-700 hover:bg-teal-100 dark:bg-teal-900/30 dark:text-teal-300 dark:hover:bg-teal-900/50"
          >
            <UploadCloud className="h-4 w-4" />
            Upload Syllabus
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
              />
            </Suspense>
          ) : (
            <div className="grid h-full min-h-[480px] place-items-center rounded-md border border-slate-200 bg-panel px-8 text-center text-sm text-slate-500">
              Ask a question to load the conceptual prerequisite map.
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

  graphContext.forEach((item, itemIndex) => {
    const conceptId = item.concept.id ?? `concept-${itemIndex}`;
    nodes.set(conceptId, {
      id: conceptId,
      label: item.concept.name ?? conceptId,
      type: item.concept.type,
      description: item.concept.description,
    });

    (item.related_concepts ?? item.prerequisites).forEach((relatedConcept, relatedIndex) => {
      const relatedId =
        relatedConcept.id ?? `${conceptId}-related-${relatedIndex}`;
      nodes.set(relatedId, {
        id: relatedId,
        label: relatedConcept.name ?? relatedId,
        type: relatedConcept.type,
        description: relatedConcept.description,
      });

    });

    item.relationships.forEach((relationship) => {
      if (!nodes.has(relationship.source) || !nodes.has(relationship.target)) return;
      const edgeId = `${relationship.source}->${relationship.target}:${relationship.type}`;
      edges.set(edgeId, {
        id: edgeId,
        source: relationship.source,
        target: relationship.target,
        label: relationship.type,
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
