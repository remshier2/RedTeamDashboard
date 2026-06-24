// Wire-format types that match the Pydantic schemas in app/schemas/*.

export type EngagementStatus = "active" | "archived" | "flushed";

export type APIKeyScope = "viewer" | "cli" | "admin";

// What GET /api-keys/me returns. The viewer calls this per Source to learn
// the key's scope so it can render mutation surfaces conditionally.
export interface APIKeyInfo {
  id: string;
  name: string;
  scope: APIKeyScope;
  created_by: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

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
  description: string | null;
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

export type FindingPhase =
  | "osint"
  | "vuln_scan"
  | "exploit"
  | "phishing"
  | "general";

export type FindingValidationStatus =
  | "pending_validation"
  | "validated"
  | "rejected"
  | "false_positive";

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
  summary?: string | null;
  phase: FindingPhase;
  status: FindingValidationStatus;
  validated_at: string | null;
  created_at: string;
}

// Payload for POST /engagements/{slug}/findings/import
export interface FindingImport {
  title: string;
  severity?: Severity;
  phase?: FindingPhase;
  summary?: string;
  target?: string;
  source_tool?: string;
  details?: Record<string, unknown>;
}

// Response shape for POST /engagements/{slug}/findings/import/nessus
// (Phase 10 — .nessus v2 XML upload).
export interface NessusImportResult {
  imported: Finding[];
  skipped_info: number;
  skipped_out_of_scope: number;
  total_items: number;
}

// Phase 10 — stored entities (Maltego import target + future sources).
// Complements the existing derived-from-findings Entity (above).
export interface StoredEntity {
  id: string;
  type: string;
  value: string;
  properties: Record<string, unknown>;
  source_tool: string;
  source_attribution: string | null;
  created_at: string;
  updated_at: string;
}

export interface MaltegoImportResult {
  inserted: number;
  merged: number;
  skipped_empty: number;
  skipped_unknown: number;
  total_nodes: number;
  entities: StoredEntity[];
}

// Phase 10 — workflow templates (starter packs).
export interface WorkflowTemplateStep {
  tool: string;
  kind: string; // TaskKind value: "scan" | "enum" | "exploit"
  owner_eligibility: string; // "agent" | "analyst" | "either"
  title: string;
  rationale?: string | null;
}

export interface WorkflowTemplate {
  id: string;
  name: string;
  description: string | null;
  is_system: boolean;
  target_kind: string; // "domain" | "cidr" | "url" | "ip"
  steps: WorkflowTemplateStep[];
}

export interface AppliedTask {
  id: string;
  title: string;
  kind: string;
  owner_eligibility: string;
  status: string;
  payload: Record<string, unknown>;
}

export interface ApplyTemplateResponse {
  template_id: string;
  template_name: string;
  target: string;
  tasks: AppliedTask[];
}

// Attachment metadata (raw bytes fetched separately via GET /attachments/{id})
export interface Attachment {
  id: string;
  finding_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
}

export type EntityType =
  | "email"
  | "ip"
  | "cidr"
  | "domain"
  | "subdomain"
  | "url"
  | "host";

export interface EntityFindingRef {
  id: string;
  title: string;
  tool: string | null;
  severity: Severity;
  phase: FindingPhase;
}

// Correlated entity derived from findings (GET /engagements/{slug}/entities).
export interface Entity {
  type: string;
  value: string;
  count: number;
  severity: Severity;
  first_seen: string;
  last_seen: string;
  findings: EntityFindingRef[];
}

export interface Observation {
  id: string;
  content: string;
  phase: FindingPhase | null;
  created_by: string | null;
  created_at: string;
}

// ─── BYO provider keys ─────────────────────────────────────────────────────

export type ProviderKeyKind = "model_provider" | "mcp_server";

export interface ProviderKey {
  id: string;
  user_id: string;
  kind: ProviderKeyKind;
  name: string;
  provider: string;
  is_local: boolean;
  models: string[];
  endpoint: string | null;
  key_last4: string | null;
  extra: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

// ─── Phase 9 orchestrator ──────────────────────────────────────────────────

export type TaskKind = "scan" | "enum" | "exploit";
export type OwnerEligibility = "agent" | "analyst" | "either";
export type TaskStatus =
  | "pending"
  | "dispatched"
  | "running"
  | "completed"
  | "failed"
  | "deferred"
  | "cancelled";

export interface Task {
  id: string;
  engagement_id: string;
  finding_id: string | null;
  title: string;
  kind: TaskKind;
  owner_eligibility: OwnerEligibility;
  status: TaskStatus;
  payload: Record<string, unknown>;
  run_id: string | null;
  dispatched_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProviderKeyEntry {
  name: string;
  provider: string;
  kind?: ProviderKeyKind;
  models?: string[];
  is_local?: boolean;
  endpoint?: string | null;
  api_key?: string | null;
  extra?: Record<string, unknown>;
}

export interface ProviderKeyImportPayload {
  providers: ProviderKeyEntry[];
}

export interface ProviderKeyImportErrorRow {
  index: number;
  name: string | null;
  reason: string;
}

export interface ProviderKeyImportResult {
  created: ProviderKey[];
  errors: ProviderKeyImportErrorRow[];
  duplicates: ProviderKeyImportErrorRow[];
}

export type SuggestionKind = "task" | "ephemeral" | "note";
export type SuggestionStatus = "open" | "accepted" | "dismissed";
export type AgentName = "strategic" | "tactical";

export interface Suggestion {
  id: string;
  engagement_id: string;
  finding_id: string | null;
  title: string;
  body: string | null;
  kind: SuggestionKind;
  payload: Record<string, unknown>;
  status: SuggestionStatus;
  created_by_agent: AgentName;
  decided_by: string | null;
  decided_at: string | null;
  task_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnalyzeFindingResponse {
  execution_id: string;
  suggestions: Suggestion[];
}

export interface AcceptSuggestionResponse {
  suggestion: Suggestion;
  task: Task | null;
  dispatched: boolean;
}

// ─── Scope bulk-import ─────────────────────────────────────────────────────

export interface ScopeImportPreviewRow {
  line: number;
  value: string;
  kind: ScopeKind;
  is_exclusion: boolean;
}

export interface ScopeImportErrorRow {
  line: number;
  raw: string;
  reason: string;
}

export interface ScopeImportDuplicateRow {
  line: number;
  value: string;
  kind: ScopeKind;
  is_exclusion: boolean;
}

export interface ScopeImportPreview {
  preview: ScopeImportPreviewRow[];
  errors: ScopeImportErrorRow[];
  would_create: number;
}

export interface ScopeImportResult {
  created: ScopeItem[];
  errors: ScopeImportErrorRow[];
  duplicates: ScopeImportDuplicateRow[];
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
      phase: FindingPhase;
      status: FindingValidationStatus;
    }
  | { type: "run.completed"; thread_id: string }
  | { type: "run.errored"; thread_id: string; error: string };

export type RunEventType = RunEvent["type"];

// ─── Costs (Phase 11) ───────────────────────────────────────────────────────

export type AgentCostName = "strategic" | "tactical";

export interface CostBucket {
  executions: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
}

export interface AgentCost extends CostBucket {
  agent: AgentCostName;
}

export interface ModelCost extends CostBucket {
  provider: string | null;
  model: string | null;
  priced: boolean;
}

export interface CostRollup {
  engagement_id: string;
  engagement_slug: string;
  total: CostBucket;
  by_agent: AgentCost[];
  by_model: ModelCost[];
  unpriced_models: string[];
}
