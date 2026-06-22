"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Upload, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  acceptSuggestion,
  analyzeFinding,
  deleteAttachment,
  dismissSuggestion,
  listAttachments,
  loadAttachmentBlob,
  updateFinding,
  uploadAttachment,
  validateFinding,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { FindingImporter } from "@/components/finding-importer";
import type {
  Attachment,
  Finding,
  FindingPhase,
  FindingValidationStatus,
  Severity,
  Suggestion,
} from "@/lib/types";

// ── display helpers ────────────────────────────────────────────────────────

const SEVERITY_RANK: Record<Severity, number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
  info: 0,
};

// Monochrome by default; the lone ember accent is reserved for critical.
const SEVERITY_CLASS: Record<Severity, string> = {
  critical: "border-critical/50 bg-critical/15 text-critical",
  high: "border-zinc-500/40 text-zinc-100",
  medium: "border-zinc-600/40 text-zinc-300",
  low: "border-zinc-700/40 text-zinc-400",
  info: "border-zinc-800 text-zinc-500",
};

const STATUS_LABEL: Record<FindingValidationStatus, string> = {
  pending_validation: "Pending",
  validated: "Validated",
  rejected: "Rejected",
  false_positive: "False positive",
};

const PHASE_LABEL: Record<FindingPhase, string> = {
  osint: "OSINT",
  vuln_scan: "Vuln Scan",
  exploit: "Exploit",
  phishing: "Phishing",
  general: "General",
};

const PHASE_FILTERS: (FindingPhase | "all")[] = [
  "all",
  "osint",
  "vuln_scan",
  "exploit",
  "phishing",
];

const STATUS_FILTERS: (FindingValidationStatus | "all")[] = [
  "all",
  "pending_validation",
  "validated",
];

function shortId(id: string): string {
  return id.replace(/-/g, "").slice(0, 6).toUpperCase();
}

// ── component ────────────────────────────────────────────────────────────

export function FindingsView({
  slug,
  findings,
  onUpdated,
}: {
  slug: string;
  findings: Finding[];
  onUpdated: (finding: Finding) => void;
}) {
  const [phase, setPhase] = useState<FindingPhase | "all">("all");
  const [status, setStatus] = useState<FindingValidationStatus | "all">("all");
  const [selected, setSelected] = useState<Finding | null>(null);
  const [showImporter, setShowImporter] = useState(false);

  const counts = {
    critical: findings.filter((f) => f.severity === "critical").length,
    high: findings.filter((f) => f.severity === "high").length,
    medlow: findings.filter((f) =>
      ["medium", "low", "info"].includes(f.severity),
    ).length,
    pending: findings.filter((f) => f.status === "pending_validation").length,
  };

  const visible = findings
    .filter((f) => phase === "all" || f.phase === phase)
    .filter((f) => status === "all" || f.status === status)
    .sort((a, b) => SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity]);

  const handleUpdated = (f: Finding) => {
    onUpdated(f);
    setSelected(f);
  };

  return (
    <div className="space-y-6">
      {/* Key metrics */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricCard label="Critical" value={counts.critical} accent />
        <MetricCard label="High" value={counts.high} />
        <MetricCard label="Med / Low" value={counts.medlow} />
        <MetricCard label="Pending validation" value={counts.pending} />
      </div>

      {/* Filters + import toggle */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <FilterRow
          options={PHASE_FILTERS}
          value={phase}
          onChange={setPhase}
          label={(v) => (v === "all" ? "All phases" : PHASE_LABEL[v])}
        />
        <FilterRow
          options={STATUS_FILTERS}
          value={status}
          onChange={setStatus}
          label={(v) =>
            v === "all" ? "All status" : STATUS_LABEL[v as FindingValidationStatus]
          }
        />
        <Button
          size="sm"
          variant="outline"
          className="ml-auto"
          onClick={() => setShowImporter((v) => !v)}
        >
          <Upload className="mr-1.5 h-3.5 w-3.5" />
          {showImporter ? "Close import" : "Import"}
        </Button>
      </div>

      {/* Inline importer panel */}
      {showImporter && (
        <FindingImporter
          slug={slug}
          onImported={(newFindings) => {
            newFindings.forEach((f) => onUpdated(f));
            setShowImporter(false);
          }}
        />
      )}

      {/* Table */}
      {visible.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No findings{findings.length ? " match these filters." : " yet."}
        </p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="px-3 py-2 w-20">ID</th>
                <th className="px-3 py-2">Finding</th>
                <th className="px-3 py-2">Detail</th>
                <th className="px-3 py-2 w-28">Status</th>
                <th className="px-3 py-2 w-24">Severity</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((f) => (
                <tr
                  key={f.id}
                  onClick={() => setSelected(f)}
                  className="cursor-pointer border-b border-border/60 align-top last:border-0 hover:bg-secondary/40"
                >
                  <td className="px-3 py-2.5 font-mono text-xs text-muted-foreground">
                    {shortId(f.id)}
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{f.title}</span>
                      {f.tool === "import" && (
                        <span className="rounded border border-border px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                          imported
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {PHASE_LABEL[f.phase]}
                      {f.tool && f.tool !== "import" ? ` · ${f.tool}` : ""}
                    </div>
                  </td>
                  <td className="px-3 py-2.5 font-mono text-xs text-muted-foreground">
                    {f.target ?? "—"}
                  </td>
                  <td className="px-3 py-2.5">
                    <span className="text-xs text-muted-foreground">
                      {STATUS_LABEL[f.status]}
                    </span>
                  </td>
                  <td className="px-3 py-2.5">
                    <Badge variant="outline" className={SEVERITY_CLASS[f.severity]}>
                      {f.severity}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <FindingSlideOver
          finding={selected}
          onClose={() => setSelected(null)}
          onUpdated={handleUpdated}
        />
      )}
    </div>
  );
}

function MetricCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: number;
  accent?: boolean;
}) {
  return (
    <div className="rounded-lg border border-border p-4">
      <div
        className={cn(
          "text-2xl font-semibold tabular-nums",
          accent && value > 0 ? "text-critical" : "text-foreground",
        )}
      >
        {value}
      </div>
      <div className="mt-1 text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
    </div>
  );
}

function FilterRow<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: T[];
  value: T;
  onChange: (v: T) => void;
  label: (v: T) => string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {options.map((opt) => (
        <button
          key={opt}
          type="button"
          onClick={() => onChange(opt)}
          className={cn(
            "rounded-full border px-2.5 py-1 text-xs transition-colors",
            value === opt
              ? "border-critical/50 bg-critical/10 text-foreground"
              : "border-border text-muted-foreground hover:text-foreground",
          )}
        >
          {label(opt)}
        </button>
      ))}
    </div>
  );
}

// ── slide-over: finding detail + validation + attack-path placeholder ──────

// ── Attachment thumbnail (fetches binary with auth, revokes URL on unmount) ──

function AttachmentThumb({
  attachment,
  onDelete,
}: {
  attachment: Attachment;
  onDelete: () => void;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!attachment.content_type.startsWith("image/")) return;
    let objectUrl: string | null = null;
    loadAttachmentBlob(attachment.id)
      .then((url) => { objectUrl = url; setSrc(url); })
      .catch(() => setSrc(null));
    return () => { if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [attachment.id, attachment.content_type]);

  const handleDelete = async () => {
    setDeleting(true);
    try {
      await deleteAttachment(attachment.id);
      onDelete();
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="group relative overflow-hidden rounded border border-border bg-background">
      {src ? (
        <img src={src} alt={attachment.filename} className="h-24 w-full object-cover" />
      ) : (
        <div className="flex h-24 items-center justify-center p-2 text-center font-mono text-[10px] text-muted-foreground">
          {attachment.filename}
        </div>
      )}
      <button
        type="button"
        onClick={handleDelete}
        disabled={deleting}
        className="absolute right-1 top-1 rounded bg-black/60 p-0.5 opacity-0 transition-opacity group-hover:opacity-100"
        aria-label="Delete attachment"
      >
        <X className="h-3 w-3 text-white" />
      </button>
      <p className="truncate px-1.5 py-0.5 text-[10px] text-muted-foreground">
        {attachment.filename}
      </p>
    </div>
  );
}

// ── slide-over ───────────────────────────────────────────────────────────────

function FindingSlideOver({
  finding,
  onClose,
  onUpdated,
}: {
  finding: Finding;
  onClose: () => void;
  onUpdated: (f: Finding) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<Suggestion[] | null>(null);
  const [dispatchedIds, setDispatchedIds] = useState<Set<string>>(new Set());
  const [decidingId, setDecidingId] = useState<string | null>(null);

  // Summary editor
  const [summary, setSummary] = useState(finding.summary ?? "");
  const [savingSummary, setSavingSummary] = useState(false);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  // Attachments
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Load attachments when the slide-over opens
  useEffect(() => {
    listAttachments(finding.id)
      .then(setAttachments)
      .catch(() => setAttachments([]));
  }, [finding.id]);

  const doSaveSummary = async () => {
    setSavingSummary(true);
    setSummaryError(null);
    try {
      const updated = await updateFinding(finding.id, { summary: summary || null });
      onUpdated(updated);
    } catch (err) {
      setSummaryError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingSummary(false);
    }
  };

  const onFileChosen = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      setUploading(true);
      setUploadError(null);
      try {
        const att = await uploadAttachment(finding.id, file);
        setAttachments((prev) => [...prev, att]);
      } catch (err) {
        setUploadError(err instanceof Error ? err.message : String(err));
      } finally {
        setUploading(false);
        e.target.value = "";
      }
    },
    [finding.id],
  );

  const decide = async (decision: FindingValidationStatus) => {
    setBusy(true);
    setError(null);
    try {
      onUpdated(await validateFinding(finding.id, decision));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  // Agents may run scan/enum paths only — never exploitation (CHARTER decided).
  const agentAllowed = finding.phase !== "exploit";

  const runAgent = async () => {
    setAnalyzing(true);
    setAnalyzeError(null);
    try {
      const res = await analyzeFinding(finding.id);
      setSuggestions(res.suggestions);
    } catch (err) {
      setAnalyzeError(err instanceof Error ? err.message : String(err));
    } finally {
      setAnalyzing(false);
    }
  };

  const acceptOne = async (s: Suggestion) => {
    setDecidingId(s.id);
    setAnalyzeError(null);
    try {
      const res = await acceptSuggestion(s.id);
      setSuggestions((prev) =>
        prev?.map((x) => (x.id === s.id ? res.suggestion : x)) ?? null,
      );
      if (res.dispatched) {
        setDispatchedIds((prev) => new Set(prev).add(s.id));
      }
    } catch (err) {
      setAnalyzeError(err instanceof Error ? err.message : String(err));
    } finally {
      setDecidingId(null);
    }
  };

  const dismissOne = async (s: Suggestion) => {
    setDecidingId(s.id);
    setAnalyzeError(null);
    try {
      const updated = await dismissSuggestion(s.id);
      setSuggestions((prev) =>
        prev?.map((x) => (x.id === s.id ? updated : x)) ?? null,
      );
    } catch (err) {
      setAnalyzeError(err instanceof Error ? err.message : String(err));
    } finally {
      setDecidingId(null);
    }
  };

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-black/60"
        onClick={onClose}
        aria-hidden
      />
      <aside className="fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col overflow-y-auto border-l border-border bg-popover p-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="font-mono text-xs text-muted-foreground">
              {shortId(finding.id)} · {PHASE_LABEL[finding.phase]}
            </div>
            <h2 className="mt-1 text-lg font-semibold leading-tight">
              {finding.title}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="mt-3 flex items-center gap-2">
          <Badge variant="outline" className={SEVERITY_CLASS[finding.severity]}>
            {finding.severity}
          </Badge>
          <span className="text-xs text-muted-foreground">
            {STATUS_LABEL[finding.status]}
          </span>
        </div>

        {finding.target && (
          <p className="mt-3 font-mono text-xs text-muted-foreground">
            target: {finding.target}
          </p>
        )}

        <pre className="mt-4 max-h-64 overflow-auto rounded-md border border-border bg-background p-3 font-mono text-xs text-muted-foreground">
          {JSON.stringify(finding.data, null, 2)}
        </pre>

        {/* Summary — analyst narrative that flows into the report */}
        <div className="mt-5">
          <h3 className="text-sm font-medium">Summary</h3>
          <Textarea
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="Write a summary for the report…"
            rows={4}
            className="mt-2 text-sm"
          />
          <div className="mt-2 flex items-center gap-2">
            <Button
              size="sm"
              disabled={savingSummary || summary === (finding.summary ?? "")}
              onClick={doSaveSummary}
            >
              {savingSummary ? "Saving…" : "Save summary"}
            </Button>
            {summaryError && (
              <p className="text-xs text-critical">{summaryError}</p>
            )}
          </div>
        </div>

        {/* Screenshots / evidence attachments */}
        <div className="mt-5">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">Screenshots</h3>
            <Button
              size="sm"
              variant="outline"
              disabled={uploading}
              onClick={() => fileRef.current?.click()}
            >
              <Upload className="mr-1.5 h-3.5 w-3.5" />
              {uploading ? "Uploading…" : "Add"}
            </Button>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={onFileChosen}
            />
          </div>
          {uploadError && (
            <p className="mt-1 text-xs text-critical">{uploadError}</p>
          )}
          {attachments.length === 0 ? (
            <p className="mt-2 text-xs text-muted-foreground">
              No screenshots attached yet.
            </p>
          ) : (
            <div className="mt-2 grid grid-cols-2 gap-2">
              {attachments.map((att) => (
                <AttachmentThumb
                  key={att.id}
                  attachment={att}
                  onDelete={() =>
                    setAttachments((prev) => prev.filter((a) => a.id !== att.id))
                  }
                />
              ))}
            </div>
          )}
        </div>

        {/* Suggested attack path — Strategic watcher (Phase 9). */}
        <div className="mt-6 rounded-md border border-dashed border-border p-4">
          <h3 className="text-sm font-medium">Suggested attack path</h3>
          <p className="mt-1 text-xs text-muted-foreground/70">
            Strategic proposes next-step tasks (scan / enum only). Accepting
            agent-eligible tasks dispatches a worker run; active tools still
            stop at the approval gate.
          </p>
          <div className="mt-3 flex gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled
              title="Analyst-driven attack path — coming next"
            >
              Analyst (manual)
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={!agentAllowed || analyzing}
              onClick={runAgent}
              title={
                agentAllowed
                  ? "Ask Strategic to propose next steps"
                  : "Agents never run exploitation — analyst only"
              }
            >
              {analyzing ? "Thinking…" : "Agent (automate)"}
            </Button>
          </div>
          {!agentAllowed && (
            <p className="mt-2 text-xs text-muted-foreground/60">
              Exploitation is analyst-only — the Agent option is disabled for
              this phase.
            </p>
          )}
          {analyzeError && (
            <p className="mt-2 text-xs text-critical">{analyzeError}</p>
          )}
          {suggestions !== null && suggestions.length === 0 && (
            <p className="mt-3 text-xs text-muted-foreground/70">
              Strategic had no follow-up tasks to propose.
            </p>
          )}
          {suggestions !== null && suggestions.length > 0 && (
            <ul className="mt-3 space-y-2">
              {suggestions.map((s) => (
                <li
                  key={s.id}
                  className="rounded-md border border-border bg-background p-3"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-sm font-medium leading-snug">
                        {s.title}
                      </p>
                      {s.body && (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {s.body}
                        </p>
                      )}
                      <p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground/60">
                        {String(s.payload.tool ?? "?")} →{" "}
                        {String(s.payload.target ?? "?")}
                        {" · "}
                        {String(s.payload.task_kind ?? "?")}
                      </p>
                    </div>
                    {s.status === "open" && (
                      <div className="flex shrink-0 gap-1">
                        <Button
                          size="sm"
                          disabled={decidingId === s.id}
                          onClick={() => acceptOne(s)}
                        >
                          Accept
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={decidingId === s.id}
                          onClick={() => dismissOne(s)}
                        >
                          Dismiss
                        </Button>
                      </div>
                    )}
                    {s.status !== "open" && (
                      <span className="shrink-0 self-center text-xs text-muted-foreground capitalize">
                        {s.status}
                        {dispatchedIds.has(s.id) ? " · dispatched" : ""}
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Validation gate */}
        <div className="mt-auto pt-6">
          {error && <p className="mb-2 text-sm text-critical">{error}</p>}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={busy || finding.status === "validated"}
              onClick={() => decide("validated")}
            >
              Validate
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => decide("rejected")}
            >
              Reject
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => decide("false_positive")}
            >
              False positive
            </Button>
          </div>
        </div>
      </aside>
    </>
  );
}
