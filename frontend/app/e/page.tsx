"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  ApprovalsModal,
  type PendingApproval,
} from "@/components/approvals-modal";
import { DownloadReport } from "@/components/download-report";
import { EventLog, type LoggedEvent } from "@/components/event-log";
import {
  FindingsTable,
  type FindingRow,
} from "@/components/findings-table";
import { GrantsCard } from "@/components/grants-card";
import { RunPrompt } from "@/components/run-prompt";
import { ScopeEditor } from "@/components/scope-editor";
import { archiveEngagement, getEngagement, listFindings } from "@/lib/api";
import { subscribeToEvents } from "@/lib/events";
import { useSources } from "@/lib/source-context";
import type { Engagement } from "@/lib/types";

// Slug comes from `?slug=...` instead of a dynamic [slug] path segment so
// the page can be statically exported for Azure Static Web Apps (dynamic
// route segments need build-time params which we don't have).

function EngagementDetail({ slug }: { slug: string }) {
  const { current } = useSources();
  const canWrite = current?.scope !== "viewer";

  const [engagement, setEngagement] = useState<Engagement | null>(null);
  const [events, setEvents] = useState<LoggedEvent[]>([]);
  const [findings, setFindings] = useState<FindingRow[]>([]);
  const [pending, setPending] = useState<PendingApproval | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<
    "connecting" | "open" | "closed"
  >("connecting");
  const [grantsRefreshKey, setGrantsRefreshKey] = useState(0);

  const seenSseIds = useRef<Set<string>>(new Set());

  const reload = useCallback(async () => {
    if (!current) return;
    try {
      setEngagement(await getEngagement(current, slug));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [current, slug]);

  useEffect(() => {
    setEngagement(null);
    setFindings([]);
    setEvents([]);
    seenSseIds.current.clear();
    reload();
  }, [reload, current?.id]);

  useEffect(() => {
    if (!current) return;
    let cancelled = false;
    listFindings(current, slug)
      .then((rows) => {
        if (cancelled) return;
        const hydrated: FindingRow[] = rows.map((r) => ({
          id: r.id,
          thread_id: r.thread_id ?? "",
          tool: r.tool ?? "",
          target: r.target,
          severity: r.severity,
          title: r.title,
          args: r.args ?? {},
          data: r.data ?? {},
        }));
        setFindings((prev) => {
          const seen = new Set(prev.map((f) => f.id));
          return [...prev, ...hydrated.filter((f) => !seen.has(f.id))];
        });
      })
      .catch(() => {
        // Non-fatal: the live stream still works.
      });
    return () => {
      cancelled = true;
    };
  }, [current, slug]);

  useEffect(() => {
    if (!current) return;
    const controller = new AbortController();
    setStreamState("connecting");
    subscribeToEvents({
      source: current,
      slug,
      signal: controller.signal,
      onOpen: () => setStreamState("open"),
      onError: () => setStreamState("closed"),
      onEvent: (event, sseId) => {
        const id = sseId ?? `local-${Date.now()}-${Math.random()}`;
        if (seenSseIds.current.has(id)) return;
        seenSseIds.current.add(id);

        setEvents((prev) => [
          { sseId: id, receivedAt: Date.now(), event },
          ...prev,
        ].slice(0, 200));

        if (event.type === "finding.created") {
          setFindings((prev) => {
            const rowId = event.finding_id || id;
            if (prev.some((f) => f.id === rowId)) return prev;
            return [
              {
                id: rowId,
                thread_id: event.thread_id,
                tool: event.tool,
                target: event.target,
                severity: event.severity,
                title: event.title,
                args: event.args,
                data: event.data,
              },
              ...prev,
            ];
          });
        } else if (event.type === "approval.pending" && canWrite) {
          setPending({
            approval_id: event.approval_id,
            thread_id: event.thread_id,
            tool: event.tool,
            args: event.args,
            risk: event.risk,
            scope: event.scope,
            tool_call_id: event.tool_call_id,
          });
        }
      },
    }).catch((err) => {
      setStreamState("closed");
      setError(err instanceof Error ? err.message : String(err));
    });

    return () => {
      controller.abort();
    };
  }, [current, slug, canWrite]);

  const onArchive = async () => {
    if (!current || !engagement) return;
    if (!window.confirm(`Archive ${engagement.slug}? Stops new runs.`)) return;
    try {
      await archiveEngagement(current, slug);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  if (!current) {
    return (
      <p className="text-sm text-muted-foreground">
        Select a source to view this engagement.
      </p>
    );
  }

  if (!engagement) {
    return (
      <p className="text-sm text-muted-foreground">
        {error ?? "Loading engagement…"}
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div>
            <Link
              href="/"
              className="text-xs text-muted-foreground hover:underline"
            >
              ← all engagements
            </Link>
            <CardTitle className="mt-2">{engagement.name}</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              slug <code>{engagement.slug}</code> · status{" "}
              <code>{engagement.status}</code> · stream{" "}
              <code>{streamState}</code> · source{" "}
              <code>{current.name}</code>
            </p>
          </div>
          <div className="flex items-start gap-2">
            <DownloadReport slug={slug} />
            {canWrite && engagement.status === "active" && (
              <Button variant="outline" size="sm" onClick={onArchive}>
                Archive
              </Button>
            )}
          </div>
        </CardHeader>
        {error && (
          <CardContent>
            <p className="text-sm text-destructive">{error}</p>
          </CardContent>
        )}
      </Card>

      <ScopeEditor slug={slug} canWrite={canWrite} />

      <GrantsCard
        engagementId={engagement.id}
        refreshKey={grantsRefreshKey}
        canRevoke={canWrite}
      />

      {canWrite && engagement.status === "active" ? (
        <RunPrompt slug={slug} />
      ) : engagement.status !== "active" ? (
        <p className="text-sm text-muted-foreground">
          This engagement is {engagement.status}; runs are disabled.
        </p>
      ) : null}

      <FindingsTable findings={findings} />
      <EventLog events={events} />

      {canWrite && (
        <ApprovalsModal
          pending={pending}
          onResolved={() => {
            setPending(null);
            setGrantsRefreshKey((k) => k + 1);
          }}
        />
      )}
    </div>
  );
}

function EngagementGate() {
  const params = useSearchParams();
  const slug = params.get("slug");
  if (!slug) {
    return (
      <p className="text-sm text-muted-foreground">
        Missing <code>?slug=</code> parameter. Go back to{" "}
        <Link href="/" className="underline">
          engagements
        </Link>
        .
      </p>
    );
  }
  return <EngagementDetail slug={slug} />;
}

export default function EngagementDetailPage() {
  // useSearchParams() requires a Suspense boundary under static export.
  return (
    <Suspense
      fallback={
        <p className="text-sm text-muted-foreground">Loading…</p>
      }
    >
      <EngagementGate />
    </Suspense>
  );
}
