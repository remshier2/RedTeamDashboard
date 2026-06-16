"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
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
import { createEngagement, listEngagements } from "@/lib/api";
import type { Engagement } from "@/lib/types";

function statusVariant(status: Engagement["status"]) {
  if (status === "active") return "default" as const;
  if (status === "archived") return "secondary" as const;
  return "outline" as const;
}

export default function EngagementListPage() {
  // Single-tenant: any signed-in analyst can create/manage engagements.
  const canWrite = true;
  const [engagements, setEngagements] = useState<Engagement[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [creating, setCreating] = useState(false);

  const reload = useCallback(async () => {
    try {
      setError(null);
      setEngagements(await listEngagements());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    setEngagements(null);
    reload();
  }, [reload]);

  const onCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    try {
      await createEngagement({
        name: name.trim(),
        slug: slug.trim() || undefined,
      });
      setName("");
      setSlug("");
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="space-y-8">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Engagements</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {engagements === null
              ? "Loading…"
              : `${engagements.length} ${
                  engagements.length === 1 ? "engagement" : "engagements"
                }`}
          </p>
        </div>
      </div>

      {canWrite && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">New engagement</CardTitle>
            <CardDescription>
              Slug auto-generates from the name if you leave it blank.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form
              onSubmit={onCreate}
              className="grid gap-4 sm:grid-cols-3 sm:items-end"
            >
              <div className="space-y-2 sm:col-span-2">
                <Label htmlFor="name">Name</Label>
                <Input
                  id="name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="Acme Q1 Pentest"
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="slug">Slug (optional)</Label>
                <Input
                  id="slug"
                  value={slug}
                  onChange={(event) => setSlug(event.target.value)}
                  placeholder="acme-q1-pentest"
                />
              </div>
              <Button type="submit" className="sm:col-span-3" disabled={creating}>
                {creating ? "Creating…" : "Create engagement"}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}

      {error && <p className="text-sm text-critical">{error}</p>}

      {engagements && engagements.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">
          No engagements yet{canWrite ? " — create one above." : "."}
        </p>
      )}

      {engagements && engagements.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {engagements.map((eng) => (
            <Link
              key={eng.id}
              href={`/e?slug=${encodeURIComponent(eng.slug)}`}
              className="group rounded-lg border border-border bg-card p-5 transition-colors hover:border-muted-foreground/40"
            >
              <div className="flex items-start justify-between gap-3">
                <h2 className="font-medium leading-tight group-hover:text-foreground">
                  {eng.name}
                </h2>
                <Badge variant={statusVariant(eng.status)}>{eng.status}</Badge>
              </div>
              <p className="mt-2 font-mono text-xs text-muted-foreground">
                {eng.slug}
              </p>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
