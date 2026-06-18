"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Trash2 } from "lucide-react";
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
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ScopeImporter } from "@/components/scope-importer";
import { createEngagement, createScopeItem, startRun } from "@/lib/api";
import type { LLMProvider, ScopeKind } from "@/lib/types";

// Nessus-style engagement setup (CHARTER Idea 3): name, details, scope — then
// "Save & start OSINT" provisions the engagement, writes the scope, and kicks
// off a passive-recon run so findings start populating immediately.

const KINDS: ScopeKind[] = ["domain", "cidr", "ip", "url"];

const DEFAULT_MODELS: Record<LLMProvider, string> = {
  anthropic: "claude-opus-4-7",
  openai: "gpt-4o-mini",
  azure: "",
  ollama: "llama3.1:8b",
};

const PROVIDERS: { value: LLMProvider; label: string }[] = [
  { value: "anthropic", label: "Anthropic (Claude)" },
  { value: "openai", label: "OpenAI" },
  { value: "azure", label: "Azure OpenAI" },
  { value: "ollama", label: "Ollama (local)" },
];

const OSINT_PROMPT =
  "Run passive OSINT reconnaissance across all in-scope targets: enumerate " +
  "subdomains, resolve DNS, run WHOIS, and probe which hosts are live.";

interface ScopeDraft {
  kind: ScopeKind;
  value: string;
  isExclusion: boolean;
}

export default function NewEngagementPage() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scope, setScope] = useState<ScopeDraft[]>([]);

  const [kind, setKind] = useState<ScopeKind>("domain");
  const [value, setValue] = useState("");
  const [isExclusion, setIsExclusion] = useState(false);

  const [provider, setProvider] = useState<LLMProvider>("anthropic");
  const [model, setModel] = useState(DEFAULT_MODELS.anthropic);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const addScope = () => {
    if (!value.trim()) return;
    setScope((s) => [...s, { kind, value: value.trim(), isExclusion }]);
    setValue("");
    setIsExclusion(false);
  };

  const submit = async (startOsint: boolean) => {
    if (!name.trim()) {
      setError("Name is required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const eng = await createEngagement({
        name: name.trim(),
        description: description.trim() || undefined,
      });
      for (const item of scope) {
        await createScopeItem(eng.slug, {
          kind: item.kind,
          value: item.value,
          is_exclusion: item.isExclusion,
        });
      }
      if (startOsint) {
        if (scope.some((s) => !s.isExclusion)) {
          await startRun(eng.slug, {
            prompt: OSINT_PROMPT,
            model: { provider, name: model.trim() },
          });
        } else {
          // No includes → nothing in scope to scan; skip the run rather than
          // 400, and let the analyst add scope on the engagement page.
          setError(null);
        }
      }
      router.push(`/e?slug=${encodeURIComponent(eng.slug)}&view=findings`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  const placeholder =
    kind === "domain"
      ? "acme.com"
      : kind === "cidr"
        ? "10.0.0.0/24"
        : kind === "ip"
          ? "10.0.0.5"
          : "https://acme.com/login";

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <div>
        <Link
          href="/"
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          ← all engagements
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">
          New engagement
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Name it, set scope, and start OSINT — findings begin populating on save.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Details</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="name">Name</Label>
            <Input
              id="name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Acme Q1 Pentest"
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="description">Description / rules of engagement</Label>
            <Textarea
              id="description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              placeholder="Objectives, constraints, point of contact…"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Scope</CardTitle>
          <CardDescription>
            Targets the engagement may touch. Tool calls outside scope are denied
            by the gate. Add includes (and optional exclusions).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <ScopeImporter
            onCommit={(_text, preview) => {
              setScope((s) => [
                ...s,
                ...preview.preview.map((row) => ({
                  kind: row.kind,
                  value: row.value,
                  isExclusion: row.is_exclusion,
                })),
              ]);
            }}
          />
          <div className="grid gap-3 sm:grid-cols-[7rem_1fr_auto] sm:items-end">
            <div className="space-y-2">
              <Label htmlFor="kind">Kind</Label>
              <select
                id="kind"
                value={kind}
                onChange={(e) => setKind(e.target.value as ScopeKind)}
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="value">Value</Label>
              <Input
                id="value"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addScope();
                  }
                }}
                placeholder={placeholder}
              />
            </div>
            <Button type="button" variant="outline" onClick={addScope}>
              Add
            </Button>
            <label className="flex items-center gap-2 text-sm sm:col-span-3">
              <input
                type="checkbox"
                checked={isExclusion}
                onChange={(e) => setIsExclusion(e.target.checked)}
                className="h-4 w-4 rounded border-input"
              />
              Exclusion (carve out from a broader include)
            </label>
          </div>

          {scope.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No scope yet — add at least one include to scan on start.
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {scope.map((item, i) => (
                <li
                  key={`${item.kind}-${item.value}-${i}`}
                  className="flex items-center justify-between py-2"
                >
                  <div className="flex items-center gap-3">
                    <Badge variant={item.isExclusion ? "destructive" : "secondary"}>
                      {item.kind}
                      {item.isExclusion ? " · exclude" : ""}
                    </Badge>
                    <span className="font-mono text-sm">{item.value}</span>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => setScope((s) => s.filter((_, j) => j !== i))}
                    aria-label="Remove scope item"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">OSINT engine</CardTitle>
          <CardDescription>
            Model used for the kickoff recon run on “Save &amp; start OSINT”.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="provider">Provider</Label>
              <select
                id="provider"
                value={provider}
                onChange={(e) => {
                  const p = e.target.value as LLMProvider;
                  setProvider(p);
                  setModel(DEFAULT_MODELS[p]);
                }}
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="model">Model</Label>
              <Input
                id="model"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder={DEFAULT_MODELS[provider] || "deployment name"}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {error && <p className="text-sm text-critical">{error}</p>}

      <div className="flex justify-end gap-2">
        <Button variant="outline" disabled={busy} onClick={() => submit(false)}>
          {busy ? "Saving…" : "Save"}
        </Button>
        <Button disabled={busy} onClick={() => submit(true)}>
          {busy ? "Starting…" : "Save & start OSINT"}
        </Button>
      </div>
    </div>
  );
}
