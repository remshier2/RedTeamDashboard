"use client";

import { useCallback, useEffect, useState } from "react";
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
import { createScopeItem, deleteScopeItem, listScope } from "@/lib/api";
import type { ScopeItem, ScopeKind } from "@/lib/types";

const KINDS: ScopeKind[] = ["domain", "cidr", "ip", "url"];

export function ScopeEditor({
  slug,
  canWrite,
}: {
  slug: string;
  canWrite: boolean;
}) {
  const [items, setItems] = useState<ScopeItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [kind, setKind] = useState<ScopeKind>("domain");
  const [value, setValue] = useState("");
  const [isExclusion, setIsExclusion] = useState(false);
  const [note, setNote] = useState("");
  const [adding, setAdding] = useState(false);

  const reload = useCallback(async () => {
    try {
      setError(null);
      setItems(await listScope(slug));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [slug]);

  useEffect(() => {
    setItems(null);
    reload();
  }, [reload]);

  const onAdd = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!value.trim()) return;
    setAdding(true);
    try {
      await createScopeItem(slug, {
        kind,
        value: value.trim(),
        is_exclusion: isExclusion,
        note: note.trim() || null,
      });
      setValue("");
      setNote("");
      setIsExclusion(false);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAdding(false);
    }
  };

  const onDelete = async (id: string) => {
    try {
      await deleteScopeItem(slug, id);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Scope</CardTitle>
        <CardDescription>
          Tool calls that fall outside these items are denied by the gate
          before they ever run. Exclusions override includes.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {canWrite && (
        <form
          onSubmit={onAdd}
          className="grid gap-3 sm:grid-cols-[7rem_1fr_auto] sm:items-end"
        >
          <div className="space-y-2">
            <Label htmlFor="scope-kind">Kind</Label>
            <select
              id="scope-kind"
              value={kind}
              onChange={(event) => setKind(event.target.value as ScopeKind)}
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="scope-value">Value</Label>
            <Input
              id="scope-value"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder={
                kind === "domain"
                  ? "acme.com"
                  : kind === "cidr"
                    ? "10.0.0.0/24"
                    : kind === "ip"
                      ? "10.0.0.5"
                      : "https://acme.com/login"
              }
              required
            />
          </div>
          <Button type="submit" disabled={adding}>
            {adding ? "Adding…" : "Add"}
          </Button>
          <label className="flex items-center gap-2 text-sm sm:col-span-2">
            <input
              type="checkbox"
              checked={isExclusion}
              onChange={(event) => setIsExclusion(event.target.checked)}
              className="h-4 w-4 rounded border-input"
            />
            Exclusion (carves out from a broader include above)
          </label>
          <Input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="optional note"
            className="sm:col-span-3"
          />
        </form>
        )}

        {error && <p className="text-sm text-destructive">{error}</p>}

        {items === null && !error && (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}

        {items && items.length === 0 && (
          <p className="rounded border border-amber-500/40 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            No scope yet — runs will be silently denied until you add at least
            one include. Try <code>kind: domain · value: acme.com</code> for the
            stub tools.
          </p>
        )}

        {items && items.length > 0 && (
          <ul className="divide-y">
            {items.map((item) => (
              <li
                key={item.id}
                className="flex items-center justify-between py-2"
              >
                <div className="flex items-center gap-3">
                  <Badge
                    variant={item.is_exclusion ? "destructive" : "secondary"}
                  >
                    {item.kind}
                    {item.is_exclusion ? " · exclude" : ""}
                  </Badge>
                  <span className="font-mono text-sm">{item.value}</span>
                  {item.note && (
                    <span className="text-xs text-muted-foreground">
                      {item.note}
                    </span>
                  )}
                </div>
                {canWrite && (
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => onDelete(item.id)}
                    aria-label="Delete scope item"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
