// Typed fetch wrappers over the FastAPI surface.
//
// Browser → backend at NEXT_PUBLIC_API_BASE (compose maps the backend container
// to localhost:8000). The X-User-Id header is read from localStorage on every
// call; real auth replaces this seam later.

import { getUserId } from "@/lib/user";
import type {
  Approval,
  ApprovalStatus,
  Authorization,
  Engagement,
  EngagementStatus,
  Finding,
  RunModel,
  RunStartResponse,
  ScopeItem,
  ScopeKind,
} from "@/lib/types";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

function headers(extra?: HeadersInit): HeadersInit {
  const userId = getUserId();
  return {
    "Content-Type": "application/json",
    ...(userId ? { "X-User-Id": userId } : {}),
    ...extra,
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...headers(), ...(init?.headers ?? {}) },
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

export function listScope(slug: string): Promise<ScopeItem[]> {
  return request<ScopeItem[]>(`/engagements/${slug}/scope`);
}

export function createScopeItem(
  slug: string,
  body: {
    kind: ScopeKind;
    value: string;
    is_exclusion?: boolean;
    note?: string | null;
  },
): Promise<ScopeItem> {
  return request<ScopeItem>(`/engagements/${slug}/scope`, {
    method: "POST",
    body: JSON.stringify(body),
  });
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
// Authorizations (session-grants)
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
  return request<Authorization>(
    `/authorizations/${authorizationId}/revoke`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------------
// Reports
// ---------------------------------------------------------------------------

export async function downloadEngagementReport(slug: string): Promise<void> {
  const userId = getUserId();
  const response = await fetch(`${BASE}/engagements/${slug}/report`, {
    headers: { ...(userId ? { "X-User-Id": userId } : {}) },
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

export const API_BASE = BASE;
