"use client";

import { useCallback, useEffect, useState } from "react";
import { Lock, Play, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  applyWorkflowTemplate,
  listWorkflowTemplates,
} from "@/lib/api";
import type {
  ApplyTemplateResponse,
  WorkflowTemplate,
} from "@/lib/types";

const TARGET_PLACEHOLDER: Record<string, string> = {
  domain: "acme.example.com",
  url: "https://acme.example.com",
  cidr: "10.0.0.0/24",
  ip: "10.0.0.5",
};

function ApplyForm({
  template,
  slug,
  onApplied,
}: {
  template: WorkflowTemplate;
  slug: string;
  onApplied: (result: ApplyTemplateResponse) => void;
}) {
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async () => {
    if (!target.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await applyWorkflowTemplate(slug, template.id, target.trim());
      onApplied(result);
      setTarget("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-end gap-2">
        <div className="flex-1">
          <label className="mb-1 block text-xs text-muted-foreground">
            Target <span className="font-mono">({template.target_kind})</span>
          </label>
          <Input
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder={TARGET_PLACEHOLDER[template.target_kind] ?? "target"}
            onKeyDown={(e) => {
              if (e.key === "Enter") onSubmit();
            }}
          />
        </div>
        <Button
          type="button"
          size="sm"
          disabled={busy || !target.trim()}
          onClick={onSubmit}
        >
          <Play className="mr-1.5 h-3.5 w-3.5" />
          {busy ? "Applying…" : "Apply"}
        </Button>
      </div>
      {error && <p className="text-xs text-critical">{error}</p>}
    </div>
  );
}

function TemplateCard({
  template,
  slug,
  onApplied,
}: {
  template: WorkflowTemplate;
  slug: string;
  onApplied: (result: ApplyTemplateResponse) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2 text-base">
              {template.is_system ? (
                <Lock
                  className="h-3.5 w-3.5 text-muted-foreground"
                  aria-label="System template"
                />
              ) : (
                <Sparkles
                  className="h-3.5 w-3.5 text-muted-foreground"
                  aria-label="User template"
                />
              )}
              {template.name}
            </CardTitle>
            {template.description && (
              <CardDescription>{template.description}</CardDescription>
            )}
          </div>
          <Badge variant="outline" className="shrink-0 font-mono text-[10px]">
            {template.steps.length} step
            {template.steps.length === 1 ? "" : "s"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          {expanded ? "Hide steps" : "Show steps"}
        </button>
        {expanded && (
          <ul className="space-y-1 rounded border border-border bg-background p-2 text-xs">
            {template.steps.map((step, i) => (
              <li
                key={i}
                className="flex items-center gap-2 border-b border-border/40 py-1 last:border-0"
              >
                <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                  {i + 1}.
                </span>
                <span className="shrink-0 font-mono text-[10px]">
                  {step.tool}
                </span>
                <span className="truncate">{step.title}</span>
                <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground">
                  {step.kind}
                </span>
              </li>
            ))}
          </ul>
        )}
        <ApplyForm template={template} slug={slug} onApplied={onApplied} />
      </CardContent>
    </Card>
  );
}

export function WorkflowTemplatesView({ slug }: { slug: string }) {
  const [templates, setTemplates] = useState<WorkflowTemplate[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [lastApplied, setLastApplied] = useState<ApplyTemplateResponse | null>(
    null,
  );

  const reload = useCallback(async () => {
    try {
      const rows = await listWorkflowTemplates();
      setTemplates(rows);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  if (loadError)
    return <p className="text-sm text-critical">{loadError}</p>;
  if (templates === null)
    return (
      <p className="text-sm text-muted-foreground">Loading templates…</p>
    );

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <h2 className="text-base font-medium">Workflow templates</h2>
        <p className="text-sm text-muted-foreground">
          Apply a starter pack against a target to mint pending Tasks. The
          analyst still dispatches each Task from the orchestrator queue.
        </p>
      </div>

      {lastApplied && (
        <div className="rounded border border-border bg-secondary/40 p-3 text-sm">
          <p>
            Applied{" "}
            <span className="font-medium">{lastApplied.template_name}</span>{" "}
            to <span className="font-mono">{lastApplied.target}</span> —
            created{" "}
            <span className="font-mono">{lastApplied.tasks.length}</span>{" "}
            task{lastApplied.tasks.length === 1 ? "" : "s"}{" "}
            <span className="text-muted-foreground">(status=pending)</span>.
          </p>
        </div>
      )}

      {templates.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No templates yet. The starter set seeds on backend startup.
        </p>
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {templates.map((tpl) => (
            <TemplateCard
              key={tpl.id}
              template={tpl}
              slug={slug}
              onApplied={(result) => setLastApplied(result)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
