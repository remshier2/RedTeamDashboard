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
import { useSources } from "@/lib/source-context";
import type { Engagement } from "@/lib/types";

export default function EngagementListPage() {
  const { current } = useSources();
  const canWrite = current?.scope !== "viewer";
  const [engagements, setEngagements] = useState<Engagement[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [creating, setCreating] = useState(false);

  const reload = useCallback(async () => {
    if (!current) return;
    try {
      setError(null);
      setEngagements(await listEngagements(current));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [current]);

  useEffect(() => {
    setEngagements(null);
    reload();
  }, [reload, current?.id]);

  const onCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!current) return;
    if (!name.trim()) return;
    setCreating(true);
    try {
      await createEngagement(current, {
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

  if (!current) {
    return (
      <p className="text-sm text-muted-foreground">
        Select a source to view engagements.
      </p>
    );
  }

  return (
    <div className="space-y-6">
      {canWrite && (
        <Card>
          <CardHeader>
            <CardTitle>New engagement</CardTitle>
            <CardDescription>
              Slug auto-generates from the name if you leave it blank.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={onCreate} className="grid gap-4 sm:grid-cols-3 sm:items-end">
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

      <Card>
        <CardHeader>
          <CardTitle>Engagements</CardTitle>
          <CardDescription>
            Source <code>{current.name}</code>
            {current.scope ? ` · key scope: ${current.scope}` : ""}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {error && (
            <p className="mb-3 text-sm text-destructive">{error}</p>
          )}
          {engagements === null && !error && (
            <p className="text-sm text-muted-foreground">Loading…</p>
          )}
          {engagements && engagements.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No engagements yet
              {canWrite ? " — create one above." : "."}
            </p>
          )}
          {engagements && engagements.length > 0 && (
            <ul className="divide-y">
              {engagements.map((eng) => (
                <li
                  key={eng.id}
                  className="flex items-center justify-between py-3"
                >
                  <div>
                    <Link
                      href={`/e?slug=${encodeURIComponent(eng.slug)}`}
                      className="font-medium hover:underline"
                    >
                      {eng.name}
                    </Link>
                    <p className="text-xs text-muted-foreground">{eng.slug}</p>
                  </div>
                  <Badge
                    variant={
                      eng.status === "active"
                        ? "default"
                        : eng.status === "archived"
                          ? "secondary"
                          : "outline"
                    }
                  >
                    {eng.status}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
