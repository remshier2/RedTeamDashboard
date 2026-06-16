"use client";

import { useState } from "react";
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
import { startRun } from "@/lib/api";
import type { LLMProvider } from "@/lib/types";

// Default model names by provider. Pre-filled when the user picks a
// provider; the actual model name is free-form (no backend whitelist) so
// rotating to a new release is just a text edit, not a code change.
const DEFAULT_MODELS: Record<LLMProvider, string> = {
  anthropic: "claude-opus-4-7",
  openai: "gpt-4o-mini",
  azure: "",
  ollama: "llama3.1:8b",
};

const PROVIDER_OPTIONS: { value: LLMProvider; label: string }[] = [
  { value: "anthropic", label: "Anthropic (Claude)" },
  { value: "openai", label: "OpenAI" },
  { value: "azure", label: "Azure OpenAI" },
  { value: "ollama", label: "Ollama (local)" },
];

export function RunPrompt({
  slug,
  onStarted,
}: {
  slug: string;
  onStarted?: (threadId: string) => void;
}) {
  const [prompt, setPrompt] = useState(
    "enumerate acme.com subdomains and probe what's live",
  );
  const [provider, setProvider] = useState<LLMProvider>("anthropic");
  const [modelName, setModelName] = useState<string>(DEFAULT_MODELS.anthropic);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onProviderChange = (next: LLMProvider) => {
    setProvider(next);
    setModelName(DEFAULT_MODELS[next]);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!prompt.trim()) return;
    if (!modelName.trim()) {
      setError("model name is required");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await startRun(slug, {
        prompt: prompt.trim(),
        model: { provider, name: modelName.trim() },
      });
      onStarted?.(result.thread_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Start a run</CardTitle>
        <CardDescription>
          Pushes <code>run.start</code> onto the inbound stream. The worker
          picks it up; events stream into the panels below.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="prompt">Prompt</Label>
            <Textarea
              id="prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              rows={3}
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="provider">Provider</Label>
              <select
                id="provider"
                value={provider}
                onChange={(event) =>
                  onProviderChange(event.target.value as LLMProvider)
                }
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                {PROVIDER_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="model-name">Model</Label>
              <Input
                id="model-name"
                value={modelName}
                onChange={(event) => setModelName(event.target.value)}
                placeholder={DEFAULT_MODELS[provider] || "deployment name"}
              />
            </div>
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          <Button type="submit" disabled={busy}>
            {busy ? "Sending…" : "Run"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
