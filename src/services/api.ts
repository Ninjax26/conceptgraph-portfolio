export interface ConceptNode {
  id: string;
  name: string;
  type: string;
  description: string;
}

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
    displayed_edges: number;
    filter_reason: string;
  };
  confidence: {
    level: "high" | "medium" | "low" | "insufficient";
    score: number;
    evidence_count: number;
    reason: string;
  };
}

export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1";

async function fetchWithTimeout(url: string, options: RequestInit & { timeout?: number } = {}) {
  const { timeout = 30000, ...requestOptions } = options;
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  
  try {
    const response = await fetch(url, {
      credentials: "include",
      ...requestOptions,
      signal: controller.signal,
    });
    clearTimeout(id);
    if (!response.ok) {
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
  const response = await fetchWithTimeout(`${API_BASE_URL}/auth/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access_code: accessCode }),
    timeout: 15000,
  });
  return response.json() as Promise<AuthSessionStatus>;
}

export async function deleteAuthSession(): Promise<void> {
  await fetchWithTimeout(`${API_BASE_URL}/auth/session`, {
    method: "DELETE",
    timeout: 15000,
  });
}

export async function sendQuery(
  question: string,
  courseId: string,
): Promise<QueryResponse> {
  const response = await fetchWithTimeout(`${API_BASE_URL}/query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      question,
      course_id: courseId,
    }),
    timeout: 60000, // 60s timeout for LLM synthesis
  });

  return response.json() as Promise<QueryResponse>;
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
  const response = await fetchWithTimeout(`${API_BASE_URL}/exam/generate`, {
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

  return response.json() as Promise<ExamResponse>;
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

  const response = await fetchWithTimeout(`${API_BASE_URL}/ingest/upload`, {
    method: "POST",
    body: formData,
    timeout: 30000,
  });

  return response.json() as Promise<IngestResponse>;
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
  last_updated_at?: string | null;
  historical_records: number;
  duplicate_records: number;
}

export async function getUploadStatus(taskId: string): Promise<UploadStatusResponse> {
  const response = await fetchWithTimeout(`${API_BASE_URL}/ingest/status/${taskId}`, {
    method: "GET",
    timeout: 15000,
  });

  return response.json() as Promise<UploadStatusResponse>;
}

export async function listUploads(): Promise<UploadStatusResponse[]> {
  const response = await fetchWithTimeout(`${API_BASE_URL}/ingest/uploads`, {
    method: "GET",
    timeout: 15000,
  });

  return response.json() as Promise<UploadStatusResponse[]>;
}

export async function listCourses(): Promise<CourseSummary[]> {
  const response = await fetchWithTimeout(`${API_BASE_URL}/ingest/courses`, {
    method: "GET",
    timeout: 15000,
  });
  return response.json() as Promise<CourseSummary[]>;
}

export async function retryUpload(uploadId: string): Promise<IngestResponse> {
  const response = await fetchWithTimeout(
    `${API_BASE_URL}/ingest/uploads/${uploadId}/retry`,
    { method: "POST", timeout: 15000 },
  );
  return response.json() as Promise<IngestResponse>;
}

export async function removeUpload(uploadId: string): Promise<void> {
  await fetchWithTimeout(`${API_BASE_URL}/ingest/uploads/${uploadId}`, {
    method: "DELETE",
    timeout: 30000,
  });
}
