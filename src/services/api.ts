export interface ConceptNode {
  id: string;
  name: string;
  type: string;
  description: string;
  normalized_name?: string;
  source_chunk_id?: string;
  document_name?: string;
  page_number?: number | null;
  section_heading?: string;
  upload_id?: string;
  source_chunk_ids?: string[];
  page_numbers?: number[];
  section_headings?: string[];
  retrieval_hop?: 0 | 1 | 2;
}

export type GraphStatus = "GRAPH_READY" | "GRAPH_PARTIAL" | "READY_WITHOUT_GRAPH";

export interface ConceptRelationship {
  source_node_id: string;
  target_node_id: string;
  relation_type: string;
}

export interface SourceChunk {
  source_id: string;
  document_id: string;
  document_name: string;
  page_number: number | null;
  section_heading: string | null;
  supporting_passage: string;
  source_type: "pdf";
  preview_url?: string | null;
  metadata: Record<string, string | number | boolean | null>;
}

export interface GraphRelationship {
  source: string;
  target: string;
  type: string;
  direction: string;
  upload_id?: string;
  document_name?: string;
}

export interface GraphContextItem {
  concept: Partial<ConceptNode>;
  related_concepts: Array<Partial<ConceptNode>>;
  prerequisites: Array<Partial<ConceptNode>>;
  one_hop_prerequisites?: Array<Partial<ConceptNode>>;
  two_hop_prerequisites?: Array<Partial<ConceptNode>>;
  relationships: GraphRelationship[];
}

export interface QueryResponse {
  answer: string;
  sources: SourceChunk[];
  graph_context: GraphContextItem[];
  graph_metadata: {
    total_nodes: number;
    total_edges: number;
    displayed_nodes: number;
    display_budget_applied?: boolean;
    display_node_limit?: number;
    display_edge_limit?: number;
    displayed_edges: number;
    filter_reason: string;
    graph_status: GraphStatus;
    retrieval_mode?: "vector_only" | "one_hop" | "two_hop" | "visualization_only";
    requested_mode?: RetrievalMode;
    fallback_reason?: string;
    projection_missing?: boolean;
    candidates?: {
      candidate_limit: number;
      embedding_execution?: "local" | "hosted_in_dense_search";
      timings?: Record<string, number>;
      dense_count?: number;
      identifier_count?: number;
      merged_count?: number;
      identifier_terms_count?: number;
      identifier_lookup_used?: boolean;
      fallback_reason?: string | null;
    };
    timings?: Record<string, number>;
    graph_expansion?: {
      anchor_match_found: boolean;
      maximum_hops: number;
      one_hop_count: number;
      two_hop_count: number;
    };
  };
  confidence: {
    level: "high" | "medium" | "low" | "insufficient";
    score: number;
    evidence_count: number;
    reason: string;
  };
  generation_metadata: {
    provider_used: "groq" | "gemini" | "cerebras" | "evidence_fallback" | "not_attempted";
    failover_used: boolean;
    failover_reason?: string | null;
  };
}

export interface ProviderStatusResponse {
  shared_budget?: {
    enabled: boolean;
    requests_per_minute: number;
    requests_per_day: number;
    usage: Array<{ provider: string; minute_requests: number; day_requests: number }>;
    note: string;
  };
  primary: string;
  fallback_order: string[];
  providers: Array<{
    name: string;
    configured: boolean;
    available: boolean;
    cooldown_seconds: number;
    last_outcome: "success" | "failure" | null;
    last_failure_category: string | null;
    last_checked_at: string | null;
  }>;
  metrics: {
    scope: "current_api_process";
    started_at: string;
    generated_at: string;
    retained_event_limit: number;
    totals: {
      provider_attempts: number;
      successful_attempts: number;
      failed_attempts: number;
      failover_attempts: number;
      repair_attempts?: number;
      rate_limit_errors: number;
      evidence_fallbacks: number;
      estimated_input_tokens: number;
      estimated_output_tokens: number;
      average_latency_ms: number;
      p95_latency_ms: number;
    };
    by_provider: Array<{
      name: string;
      attempts: number;
      successful_attempts: number;
      failed_attempts: number;
      failover_attempts: number;
      rate_limit_errors: number;
      estimated_input_tokens: number;
      estimated_output_tokens: number;
      average_latency_ms: number;
    }>;
    by_operation: Array<{
      name: string;
      attempts: number;
      successful_attempts: number;
      failed_attempts: number;
      failover_attempts: number;
      rate_limit_errors: number;
      estimated_input_tokens: number;
      estimated_output_tokens: number;
      average_latency_ms: number;
    }>;
    recent_events: Array<{
      provider: string;
      operation: string;
      outcome: string;
      latency_ms: number;
      failover: boolean;
      estimated_input_tokens: number;
      estimated_output_tokens: number;
      recorded_at: string;
      failure_reason?: string | null;
      repair?: boolean;
    }>;
    privacy_note: string;
    accuracy_note: string;
  };
  note: string;
}

export interface PublicSampleDocument {
  upload_id: string;
  filename: string;
  page_preview_url: string;
}

export interface PublicSampleResponse {
  course_id: string;
  course_name: string;
  graph_status: GraphStatus;
  documents: PublicSampleDocument[];
  graph_context: GraphContextItem[];
  graph_metadata: QueryResponse["graph_metadata"];
}

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";

export interface ReadinessStatus {
  status: "ready";
  processing_queue_depth: number;
  degraded_services: string[];
  graph_available: boolean;
  ingestion_available?: boolean;
  active_workers?: number | null;
  processing_role?: "combined" | "api";
}

async function fetchWithTimeout(url: string, options: RequestInit & { timeout?: number } = {}) {
  const { timeout = 30000, ...requestOptions } = options;
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  const headers = new Headers(requestOptions.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes((requestOptions.method ?? "GET").toUpperCase())) {
    headers.set("X-ConceptGraph-Request", "1");
  }
  
  try {
    const response = await fetch(url, {
      credentials: "include",
      ...requestOptions,
      headers,
      signal: controller.signal,
    });
    clearTimeout(id);
    if (!response.ok) {
      if (response.status === 401 && !url.includes("/auth/session")) {
        window.dispatchEvent(new Event("conceptgraph:session-expired"));
      }
      let message = response.statusText;
      const errorBody = await response.text();
      if (errorBody) {
        try {
          const errJson = JSON.parse(errorBody) as { detail?: unknown };
          message =
            typeof errJson.detail === "string"
              ? errJson.detail
              : JSON.stringify(errJson.detail ?? errJson);
        } catch {
          message = errorBody;
        }
      }
      throw new Error(`API request failed: ${message}`);
    }
    return response;
  } catch (error) {
    clearTimeout(id);
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out. The server took too long to respond.");
    }
    if (error instanceof TypeError && error.message === "Failed to fetch") {
      throw new Error("Network error: Could not connect to the server. Is it running?");
    }
    throw error;
  }
}

async function fetchJson<T>(url: string, options: RequestInit & { timeout?: number }): Promise<T> {
  const response = await fetchWithTimeout(url, options);
  return response.json() as Promise<T>;
}

export interface AuthSessionStatus {
  enabled: boolean;
  authenticated: boolean;
  expires_in_seconds: number | null;
}

export async function getAuthSession(): Promise<AuthSessionStatus> {
  const response = await fetch(`${API_BASE_URL}/auth/session`, {
    method: "GET",
    credentials: "include",
    cache: "no-store",
  });
  if (response.status === 401) {
    return { enabled: true, authenticated: false, expires_in_seconds: null };
  }
  if (!response.ok) {
    throw new Error("Could not verify dashboard access.");
  }
  return response.json() as Promise<AuthSessionStatus>;
}

export async function createAuthSession(accessCode: string): Promise<AuthSessionStatus> {
  return fetchJson<AuthSessionStatus>(`${API_BASE_URL}/auth/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access_code: accessCode }),
    timeout: 15000,
  });
}

export async function deleteAuthSession(): Promise<void> {
  await fetchWithTimeout(`${API_BASE_URL}/auth/session`, {
    method: "DELETE",
    timeout: 15000,
  });
}

export async function getPublicSample(): Promise<PublicSampleResponse> {
  return fetchJson<PublicSampleResponse>(`${API_BASE_URL}/public/sample`, {
    method: "GET",
    timeout: 15000,
  });
}

export type RetrievalMode = "vector_only" | "one_hop" | "two_hop";

export async function sendQuery(
  question: string,
  courseId: string,
  retrievalMode: RetrievalMode = "two_hop",
): Promise<QueryResponse> {
  return fetchJson<QueryResponse>(`${API_BASE_URL}/query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      question,
      course_id: courseId,
      retrieval_mode: retrievalMode,
    }),
    timeout: 60000, // 60s timeout for LLM synthesis
  });
}

export async function getProviderStatus(): Promise<ProviderStatusResponse> {
  return fetchJson<ProviderStatusResponse>(`${API_BASE_URL}/providers/status`, {
    method: "GET",
    timeout: 15000,
  });
}

export async function getReadiness(): Promise<ReadinessStatus> {
  return fetchJson<ReadinessStatus>(`${API_BASE_URL}/ready`, {
    method: "GET",
    cache: "no-store",
    timeout: 12000,
  });
}

export interface MockQuestion {
  question_text: string;
  options: string[];
  correct_answer: string;
  explanation: string;
  topic: string;
  sources: ExamSource[];
}

export interface ExamSource {
  source_id: string;
  document_name: string;
  page_number: number | null;
  section_heading: string | null;
  supporting_passage: string;
}

export interface ExamResponse {
  course_id: string;
  questions: MockQuestion[];
  source_count: number;
  coverage: {
    topics?: string[];
    documents?: string[];
    pages_by_document?: Record<string, number[]>;
  };
}

export async function generateExam(
  courseId: string,
  numQuestions: number = 5
): Promise<ExamResponse> {
  return fetchJson<ExamResponse>(`${API_BASE_URL}/exam/generate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      course_id: courseId,
      num_questions: numQuestions,
    }),
    timeout: 60000, // 60s timeout for LLM exam generation
  });
}

export interface IngestResponse {
  message: string;
  task_id: string;
  upload_id: string;
  course_id: string;
  course_name: string;
  original_filename: string;
  status: string;
  duplicate: boolean;
  preview_url: string;
}

export async function uploadDocument(
  file: File,
  courseId: string,
): Promise<IngestResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("course_id", courseId);

  return fetchJson<IngestResponse>(`${API_BASE_URL}/ingest/upload`, {
    method: "POST",
    body: formData,
    timeout: 30000,
  });
}

export interface UploadStatusResponse {
  upload_id: string;
  task_id: string;
  course_id: string;
  course_name: string;
  original_filename: string;
  status: "active" | "ready" | "failed" | "cancelled";
  stage: string;
  failure_category?: string | null;
  retryable: boolean;
  attempt_count: number;
  last_attempted_at?: string | null;
  last_heartbeat_at?: string | null;
  processed_chunk_count: number;
  graph_node_count: number;
  graph_edge_count: number;
  graph_status?: GraphStatus | null;
  error_message?: string | null;
  result_json?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  preview_url: string;
}

export interface CourseSummary {
  course_id: string;
  course_name: string;
  total_documents: number;
  active_documents: number;
  ready_documents: number;
  failed_documents: number;
  processed_chunk_count: number;
  graph_node_count: number;
  graph_edge_count: number;
  graph_status?: GraphStatus | null;
  graph_sections_total: number;
  graph_sections_succeeded: number;
  graph_batches_failed: number;
  last_updated_at?: string | null;
  historical_records: number;
  duplicate_records: number;
}

export async function getUploadStatus(taskId: string): Promise<UploadStatusResponse> {
  return fetchJson<UploadStatusResponse>(`${API_BASE_URL}/ingest/status/${taskId}`, {
    method: "GET",
    timeout: 15000,
  });
}

export async function listUploads(): Promise<UploadStatusResponse[]> {
  return fetchJson<UploadStatusResponse[]>(`${API_BASE_URL}/ingest/uploads`, {
    method: "GET",
    timeout: 15000,
  });
}

export interface UploadPageResponse {
  items: UploadStatusResponse[];
  next_cursor: string | null;
}

export async function listUploadPage(cursor: string | null = null): Promise<UploadPageResponse> {
  const params = new URLSearchParams({ limit: "25" });
  if (cursor) params.set("cursor", cursor);
  return fetchJson<UploadPageResponse>(`${API_BASE_URL}/ingest/uploads/page?${params}`, {
    method: "GET", timeout: 15000,
  });
}

export async function listCourses(): Promise<CourseSummary[]> {
  return fetchJson<CourseSummary[]>(`${API_BASE_URL}/ingest/courses`, {
    method: "GET",
    timeout: 15000,
  });
}

export async function retryUpload(uploadId: string): Promise<IngestResponse> {
  return fetchJson<IngestResponse>(
    `${API_BASE_URL}/ingest/uploads/${uploadId}/retry`,
    { method: "POST", timeout: 15000 },
  );
}

export async function removeUpload(uploadId: string): Promise<void> {
  await fetchWithTimeout(`${API_BASE_URL}/ingest/uploads/${uploadId}`, {
    method: "DELETE",
    timeout: 30000,
  });
}
