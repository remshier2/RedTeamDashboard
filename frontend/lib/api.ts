// Fetch wrappers over the FastAPI surface.
//
// Phase 7: one backend (API_BASE_URL), identified analyst. Auth is resolved
// per request — an Entra Bearer token when SSO is configured, else a dev
// X-User-Id header for local work. No more per-call Source argument.

import { API_BASE_URL, DEV_USER, ENTRA_ENABLED } from "@/lib/config";
import { getAccessToken } from "@/lib/msal";
import type {
  AcceptSuggestionResponse,
  AnalyzeFindingResponse,
  Approval,
  ApprovalStatus,
  Attachment,
  Authorization,
  CostRollup,
  Engagement,
  EngagementStatus,
  Entity,
  Finding,
  FindingImport,
  FindingPhase,
  FindingValidationStatus,
  Observation,
  RunModel,
  RunStartResponse,
  Severity,
  ScopeKind,
  Suggestion,
  SuggestionStatus,
  Task,
  TaskStatus,
} from "@/lib/types";

// Auth-only headers (no Content-Type — request() adds that for JSON bodies).
export async function authHeaders(): Promise<Record<string, string>> {
  if (ENTRA_ENABLED) {
    const token = await getAccessToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }
  return { "X-User-Id": DEV_USER };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(await authHeaders()),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Engagements
// ---------------------------------------------------------------------------

export function listEngagements(
  status?: EngagementStatus,
): Promise<Engagement[]> {
  const q = status ? `?status=${status}` : "";
  return request<Engagement[]>(`/engagements${q}`);
}

export function getEngagement(slug: string): Promise<Engagement> {
  return request<Engagement>(`/engagements/${slug}`);
}

export function createEngagement(body: {
  name: string;
  slug?: string;
  description?: string;
}): Promise<Engagement> {
  return request<Engagement>("/engagements", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function archiveEngagement(slug: string): Promise<Engagement> {
  return request<Engagement>(`/engagements/${slug}`, { method: "DELETE" });
}

export function flushEngagement(slug: string): Promise<void> {
  return request<void>(`/engagements/${slug}/flush`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Scope
// ---------------------------------------------------------------------------

export function listScope(slug: string) {
  return request<import("@/lib/types").ScopeItem[]>(
    `/engagements/${slug}/scope`,
  );
}

export function createScopeItem(
  slug: string,
  body: {
    kind: ScopeKind;
    value: string;
    is_exclusion?: boolean;
    note?: string | null;
  },
) {
  return request<import("@/lib/types").ScopeItem>(
    `/engagements/${slug}/scope`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function deleteScopeItem(slug: string, scopeId: string): Promise<void> {
  return request<void>(`/engagements/${slug}/scope/${scopeId}`, {
    method: "DELETE",
  });
}

export function parseScope(
  text: string,
): Promise<import("@/lib/types").ScopeImportPreview> {
  return request<import("@/lib/types").ScopeImportPreview>("/scope/parse", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

export function importScope(
  slug: string,
  text: string,
): Promise<import("@/lib/types").ScopeImportResult> {
  return request<import("@/lib/types").ScopeImportResult>(
    `/engagements/${slug}/scope/import`,
    { method: "POST", body: JSON.stringify({ text }) },
  );
}

// ---------------------------------------------------------------------------
// Findings
// ---------------------------------------------------------------------------

export function listFindings(
  slug: string,
  filters?: { phase?: FindingPhase; status?: FindingValidationStatus },
): Promise<Finding[]> {
  const q = new URLSearchParams();
  if (filters?.phase) q.set("phase", filters.phase);
  if (filters?.status) q.set("status", filters.status);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return request<Finding[]>(`/engagements/${slug}/findings${suffix}`);
}

export function listEntities(
  slug: string,
  filters?: { type?: string; q?: string },
): Promise<Entity[]> {
  const params = new URLSearchParams();
  if (filters?.type) params.set("type", filters.type);
  if (filters?.q) params.set("q", filters.q);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return request<Entity[]>(`/engagements/${slug}/entities${suffix}`);
}

export function validateFinding(
  findingId: string,
  decision: FindingValidationStatus,
  reason?: string,
): Promise<Finding> {
  return request<Finding>(`/findings/${findingId}/validate`, {
    method: "POST",
    body: JSON.stringify({ decision, reason }),
  });
}

// ---------------------------------------------------------------------------
// Observations
// ---------------------------------------------------------------------------

export function listObservations(slug: string): Promise<Observation[]> {
  return request<Observation[]>(`/engagements/${slug}/observations`);
}

export function createObservation(
  slug: string,
  body: { content: string; phase?: FindingPhase | null },
): Promise<Observation> {
  return request<Observation>(`/engagements/${slug}/observations`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function deleteObservation(observationId: string): Promise<void> {
  return request<void>(`/observations/${observationId}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------

export function startRun(
  slug: string,
  body: { prompt: string; model?: RunModel },
): Promise<RunStartResponse> {
  return request<RunStartResponse>(`/engagements/${slug}/runs`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Approvals
// ---------------------------------------------------------------------------

export function listApprovals(
  slug: string,
  status?: ApprovalStatus,
): Promise<Approval[]> {
  const q = status ? `?status=${status}` : "";
  return request<Approval[]>(`/engagements/${slug}/approvals${q}`);
}

export function decideApproval(
  approvalId: string,
  body: {
    approved: boolean;
    edited_args?: Record<string, unknown>;
    reason?: string;
    remember_for_session?: boolean;
  },
): Promise<Approval> {
  return request<Approval>(`/approvals/${approvalId}/decision`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Authorizations
// ---------------------------------------------------------------------------

export function listAuthorizations(
  engagementId: string,
  active?: boolean,
): Promise<Authorization[]> {
  const q = active === undefined ? "" : `?active=${active}`;
  return request<Authorization[]>(
    `/engagements/${engagementId}/authorizations${q}`,
  );
}

export function revokeAuthorization(
  authorizationId: string,
): Promise<Authorization> {
  return request<Authorization>(`/authorizations/${authorizationId}/revoke`, {
    method: "POST",
  });
}

// ---------------------------------------------------------------------------
// BYO provider keys (LLM + MCP)
// ---------------------------------------------------------------------------

export function listProviderKeys(): Promise<
  import("@/lib/types").ProviderKey[]
> {
  return request<import("@/lib/types").ProviderKey[]>("/me/provider-keys");
}

export function createProviderKey(
  body: import("@/lib/types").ProviderKeyEntry,
): Promise<import("@/lib/types").ProviderKey> {
  return request<import("@/lib/types").ProviderKey>("/me/provider-keys", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function importProviderKeys(
  payload: import("@/lib/types").ProviderKeyImportPayload,
): Promise<import("@/lib/types").ProviderKeyImportResult> {
  return request<import("@/lib/types").ProviderKeyImportResult>(
    "/me/provider-keys/import",
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function deleteProviderKey(keyId: string): Promise<void> {
  return request<void>(`/me/provider-keys/${keyId}`, { method: "DELETE" });
}

export function updateProviderKey(
  keyId: string,
  body: Partial<import("@/lib/types").ProviderKeyEntry>,
): Promise<import("@/lib/types").ProviderKey> {
  return request<import("@/lib/types").ProviderKey>(
    `/me/provider-keys/${keyId}`,
    { method: "PATCH", body: JSON.stringify(body) },
  );
}

// ---------------------------------------------------------------------------
// Orchestrator (Phase 9)
// ---------------------------------------------------------------------------

export function analyzeFinding(
  findingId: string,
): Promise<AnalyzeFindingResponse> {
  return request<AnalyzeFindingResponse>(`/findings/${findingId}/analyze`, {
    method: "POST",
  });
}

export function listSuggestions(
  slug: string,
  status?: SuggestionStatus,
): Promise<Suggestion[]> {
  const q = status ? `?status=${status}` : "";
  return request<Suggestion[]>(`/engagements/${slug}/suggestions${q}`);
}

export function acceptSuggestion(
  suggestionId: string,
): Promise<AcceptSuggestionResponse> {
  return request<AcceptSuggestionResponse>(
    `/suggestions/${suggestionId}/accept`,
    { method: "POST" },
  );
}

export function dismissSuggestion(suggestionId: string): Promise<Suggestion> {
  return request<Suggestion>(`/suggestions/${suggestionId}/dismiss`, {
    method: "POST",
  });
}

export function listTasks(slug: string, _status?: TaskStatus): Promise<Task[]> {
  // status filter accepted for symmetry but currently always lists all
  return request<Task[]>(`/engagements/${slug}/tasks`);
}

// ---------------------------------------------------------------------------
// Costs (Phase 11)
// ---------------------------------------------------------------------------

export function getEngagementCosts(slug: string): Promise<CostRollup> {
  return request<CostRollup>(`/engagements/${slug}/costs`);
}

// ---------------------------------------------------------------------------
// Reports (PDF export)
// ---------------------------------------------------------------------------

export async function downloadEngagementReport(slug: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/engagements/${slug}/report`, {
    headers: await authHeaders(),
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  const blob = await response.blob();
  const filename =
    _filenameFromDisposition(response.headers.get("content-disposition")) ??
    `${slug}-report.pdf`;
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}

function _filenameFromDisposition(value: string | null): string | null {
  if (!value) return null;
  const match = /filename="?([^"]+)"?/i.exec(value);
  return match ? match[1] : null;
}

// ---------------------------------------------------------------------------
// Findings import + update
// ---------------------------------------------------------------------------

export function importFindings(
  slug: string,
  findings: FindingImport[],
): Promise<Finding[]> {
  return request<Finding[]>(`/engagements/${slug}/findings/import`, {
    method: "POST",
    body: JSON.stringify(findings),
  });
}

/**
 * Upload a Tenable Nessus .nessus v2 XML export. The backend parser
 * walks ReportItems and persists each as a Finding(status=pending_validation).
 *
 * Uses FormData directly instead of the JSON ``request()`` helper because
 * the browser MUST set the multipart Content-Type with its own boundary;
 * fetch handles that automatically when ``body`` is a FormData and no
 * Content-Type header is set on the request.
 */
export async function importFindingsNessus(
  slug: string,
  file: File,
  includeInfo: boolean = false,
): Promise<import("@/lib/types").NessusImportResult> {
  const form = new FormData();
  form.append("file", file);
  const q = includeInfo ? "?include_info=true" : "";
  const response = await fetch(
    `${API_BASE_URL}/engagements/${slug}/findings/import/nessus${q}`,
    {
      method: "POST",
      body: form,
      headers: { ...(await authHeaders()) },
    },
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  return response.json() as Promise<import("@/lib/types").NessusImportResult>;
}

export function updateFinding(
  findingId: string,
  body: {
    title?: string;
    summary?: string | null;
    severity?: Severity;
    phase?: FindingPhase;
  },
): Promise<Finding> {
  return request<Finding>(`/findings/${findingId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Engagement JSON export
// ---------------------------------------------------------------------------

export async function downloadEngagementExport(slug: string): Promise<void> {
  const data = await request<Record<string, unknown>>(`/engagements/${slug}/export`);
  const json = JSON.stringify(data, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${slug}-export.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Finding attachments
// ---------------------------------------------------------------------------

export function listAttachments(findingId: string): Promise<Attachment[]> {
  return request<Attachment[]>(`/findings/${findingId}/attachments`);
}

export async function uploadAttachment(
  findingId: string,
  file: File,
): Promise<Attachment> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${API_BASE_URL}/findings/${findingId}/attachments`, {
    method: "POST",
    // No Content-Type header — browser sets multipart boundary automatically.
    headers: await authHeaders(),
    body: form,
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  return response.json() as Promise<Attachment>;
}

export async function loadAttachmentBlob(attachmentId: string): Promise<string> {
  const response = await fetch(`${API_BASE_URL}/attachments/${attachmentId}`, {
    headers: await authHeaders(),
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}

export function deleteAttachment(attachmentId: string): Promise<void> {
  return request<void>(`/attachments/${attachmentId}`, { method: "DELETE" });
}
