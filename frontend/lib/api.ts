// Fetch wrappers over the FastAPI surface.
//
// Phase 7: one backend (API_BASE_URL), identified analyst. Auth is resolved
// per request — an Entra Bearer token when SSO is configured, else a dev
// X-User-Id header for local work. No more per-call Source argument.

import { API_BASE_URL, DEV_USER, ENTRA_ENABLED } from "@/lib/config";
import { getAccessToken } from "@/lib/msal";
import type {
  Approval,
  ApprovalStatus,
  Authorization,
  Engagement,
  EngagementStatus,
  Finding,
  RunModel,
  RunStartResponse,
  ScopeKind,
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

// ---------------------------------------------------------------------------
// Findings
// ---------------------------------------------------------------------------

export function listFindings(slug: string): Promise<Finding[]> {
  return request<Finding[]>(`/engagements/${slug}/findings`);
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
