// Wire-format types that match the Pydantic schemas in app/schemas/*.

export type EngagementStatus = "active" | "archived" | "flushed";

// Per-(engagement, tool) standing session grant. A row with revoked_at=null is
// active and the gate auto-approves matching active calls.
export interface Authorization {
  id: string;
  engagement_id: string;
  tool_name: string;
  granted_by: string | null;
  note: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
  created_at: string;
  updated_at: string;
}
export type ScopeKind = "domain" | "cidr" | "ip" | "url";
export type RiskLevel = "passive" | "active" | "destructive";
export type ApprovalStatus =
  | "pending"
  | "approved"
  | "denied"
  | "edited"
  | "auto";

export interface Engagement {
  id: string;
  name: string;
  slug: string;
  status: EngagementStatus;
  created_by: string | null;
  archived_at: string | null;
  flushed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ScopeItem {
  id: string;
  engagement_id: string;
  kind: ScopeKind;
  value: string;
  is_exclusion: boolean;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export interface Approval {
  id: string;
  engagement_id: string;
  thread_id: string;
  node: string | null;
  tool_name: string;
  tool_args: Record<string, unknown>;
  risk: RiskLevel;
  scope_check: Record<string, unknown>;
  status: ApprovalStatus;
  decided_by: string | null;
  decision_args: Record<string, unknown> | null;
  authorization_id: string | null;
  decided_at: string | null;
  created_at: string;
  updated_at: string;
}

export type Severity = "info" | "low" | "medium" | "high" | "critical";

// Persisted finding as returned by GET /engagements/{slug}/findings. Mirrors
// the SSE `finding.created` event's tool/args/data so the table can render
// hydrated and live findings the same way.
export interface Finding {
  id: string;
  thread_id: string | null;
  tool: string | null;
  target: string | null;
  args: Record<string, unknown>;
  data: Record<string, unknown>;
  severity: Severity;
  title: string;
  created_at: string;
}

export type LLMProvider = "anthropic" | "openai" | "azure" | "ollama";

export interface RunModel {
  provider: LLMProvider;
  name: string;
}

export interface RunStartResponse {
  engagement_id: string;
  thread_id: string;
  events_stream: string;
  model: RunModel;
}

// SSE events emitted from the outbound stream.

export type RunEvent =
  | { type: "run.started"; thread_id: string; prompt: string }
  | {
      type: "approval.pending";
      thread_id: string;
      approval_id: string;
      tool: string;
      args: Record<string, unknown>;
      risk: RiskLevel;
      scope: Record<string, unknown>;
      tool_call_id: string;
    }
  | {
      type: "tool.denied";
      thread_id: string;
      tool: string;
      args: Record<string, unknown>;
      reason: string;
      scope: Record<string, unknown>;
    }
  | {
      type: "tool.auto_approved";
      thread_id: string;
      tool: string;
      args: Record<string, unknown>;
      risk: string;
      authorization_id: string;
    }
  | {
      type: "finding.created";
      thread_id: string;
      tool: string;
      args: Record<string, unknown>;
      data: Record<string, unknown>;
      target: string | null;
      severity: Severity;
      title: string | null;
      finding_id: string;
    }
  | { type: "run.completed"; thread_id: string }
  | { type: "run.errored"; thread_id: string; error: string };

export type RunEventType = RunEvent["type"];
