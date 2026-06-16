"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import {
  Crosshair,
  DollarSign,
  FileText,
  Fish,
  Radar,
  Search,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
import type { Engagement } from "@/lib/types";

// Slug comes from `?slug=...` instead of a dynamic [slug] path segment so
// the page can be statically exported for Azure Static Web Apps (dynamic
// route segments need build-time params which we don't have). The active
// phase tab rides in `?tab=...` for the same reason — deep-linkable, static.

const PHASE_TABS = [
  { value: "osint", label: "OSINT Recon", Icon: Search },
  { value: "vuln", label: "Vuln Scan", Icon: Radar },
  { value: "exploit", label: "Exploit", Icon: Crosshair },
  { value: "phishing", label: "Phishing", Icon: Fish },
  { value: "results", label: "Results", Icon: FileText },
  { value: "costs", label: "Costs", Icon: DollarSign },
] as const;

const VALID_TABS: Set<string> = new Set(PHASE_TABS.map((t) => t.value));

// Shell placeholder for tabs whose data model lands in a later phase. Keeps
// the structure honest about what's wired vs. roadmapped.
function PhasePanel({
  title,
  blurb,
  roadmap,
}: {
  title: string;
  blurb: string;
  roadmap: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">{blurb}</p>
        <p className="text-xs text-muted-foreground/70">
          <span className="text-critical">●</span> {roadmap}
        </p>
      </CardContent>
    </Card>
  );
}

function EngagementDetail({ slug }: { slug: string }) {
  const router = useRouter();
  const params = useSearchParams();
  // Single-tenant: any signed-in analyst can act on the engagement.
  const canWrite = true;

  const tabParam = params.get("tab");
  const activeTab = tabParam && VALID_TABS.has(tabParam) ? tabParam : "osint";
  const setTab = useCallback(
    (value: string) => {
      const next = new URLSearchParams(params.toString());
      next.set("tab", value);
      router.replace(`/e?${next.toString()}`, { scroll: false });
    },
    [params, router],
  );

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
    try {
      setEngagement(await getEngagement(slug));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [slug]);

  useEffect(() => {
    setEngagement(null);
    setFindings([]);
    setEvents([]);
    seenSseIds.current.clear();
    reload();
  }, [reload]);

  useEffect(() => {
    let cancelled = false;
    listFindings(slug)
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
  }, [slug]);

  useEffect(() => {
    const controller = new AbortController();
    setStreamState("connecting");
    subscribeToEvents({
      slug,
      signal: controller.signal,
      onOpen: () => setStreamState("open"),
      onError: () => setStreamState("closed"),
      onEvent: (event, sseId) => {
        const id = sseId ?? `local-${Date.now()}-${Math.random()}`;
        if (seenSseIds.current.has(id)) return;
        seenSseIds.current.add(id);

        setEvents((prev) =>
          [{ sseId: id, receivedAt: Date.now(), event }, ...prev].slice(0, 200),
        );

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
  }, [slug, canWrite]);

  const onArchive = async () => {
    if (!engagement) return;
    if (!window.confirm(`Archive ${engagement.slug}? Stops new runs.`)) return;
    try {
      await archiveEngagement(slug);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  if (!engagement) {
    return (
      <p className="text-sm text-muted-foreground">
        {error ?? "Loading engagement…"}
      </p>
    );
  }

  return (
    <div className="space-y-6">
      {/* Engagement header — name, status, controls. Engagement-wide, above
          the phase tabs. */}
      <Card>
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div>
            <Link
              href="/"
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              ← all engagements
            </Link>
            <CardTitle className="mt-2 text-xl">{engagement.name}</CardTitle>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              {engagement.slug} · {engagement.status} · stream {streamState}
            </p>
          </div>
          {canWrite && engagement.status === "active" && (
            <Button variant="outline" size="sm" onClick={onArchive}>
              Archive
            </Button>
          )}
        </CardHeader>
        {error && (
          <CardContent>
            <p className="text-sm text-critical">{error}</p>
          </CardContent>
        )}
      </Card>

      {/* Strategic command-center — placeholder until Phase 9. */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Strategic overview
          </CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground/70">
            <span className="text-critical">●</span> Phase 9 — the Strategic
            watcher surfaces the timeline (happened · happening · next) and a
            priced suggestions feed here.
          </p>
        </CardContent>
      </Card>

      {/* Engagement-wide context: scope + active session grants. */}
      <div className="grid gap-6 lg:grid-cols-2">
        <ScopeEditor slug={slug} canWrite={canWrite} />
        <GrantsCard
          engagementId={engagement.id}
          refreshKey={grantsRefreshKey}
          canRevoke={canWrite}
        />
      </div>

      {/* Phase tabs. */}
      <Tabs value={activeTab} onValueChange={setTab}>
        <TabsList>
          {PHASE_TABS.map(({ value, label, Icon }) => (
            <TabsTrigger key={value} value={value}>
              <span className="flex items-center gap-1.5">
                <Icon className="h-3.5 w-3.5" />
                {label}
              </span>
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="osint">
          <PhasePanel
            title="OSINT Recon"
            blurb="Passive reconnaissance — domains, subdomains, emails, exposed assets, leaked creds."
            roadmap="Phase 8 tags findings by phase; Phase 9 adds analyst-choice scanning (Tactical agent or manual) and import of theHarvester / amass / subfinder output."
          />
        </TabsContent>

        <TabsContent value="vuln" className="space-y-6">
          {/* Live workspace lands here for now — current findings are
              scan-oriented. Phase 8 splits findings across tabs by phase. */}
          {canWrite && engagement.status === "active" ? (
            <RunPrompt slug={slug} />
          ) : engagement.status !== "active" ? (
            <p className="text-sm text-muted-foreground">
              This engagement is {engagement.status}; runs are disabled.
            </p>
          ) : null}
          <FindingsTable findings={findings} />
          <EventLog events={events} />
        </TabsContent>

        <TabsContent value="exploit">
          <PhasePanel
            title="Exploit"
            blurb="Exploited hosts, payloads, proof, and post-exploitation notes."
            roadmap="Analyst-only by design — agents never exploit. Results are uploaded by the analyst, then pass the validation gate (Phase 9/10). Strategic can flag when a Kali attack-box is warranted."
          />
        </TabsContent>

        <TabsContent value="phishing">
          <PhasePanel
            title="Phishing"
            blurb="Campaigns, pretexts, and send / click / credential-capture stats."
            roadmap="Phase 9/10 — manual record plus CSV import of campaign results."
          />
        </TabsContent>

        <TabsContent value="results" className="space-y-6">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0">
              <CardTitle>Report</CardTitle>
              <DownloadReport slug={slug} />
            </CardHeader>
            <CardContent>
              <p className="text-xs text-muted-foreground/70">
                <span className="text-critical">●</span> Phase 8 aggregates
                validated findings across all phases here; Phase 11 polishes the
                PDF to cover every phase plus the cost summary.
              </p>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="costs">
          <PhasePanel
            title="Costs"
            blurb="Internal effort tracking — LLM spend, analyst labor (manual time logging), and infra."
            roadmap="Phase 11 — per-task estimate vs. actual, variance, and an engagement rollup feeding this tab and the report."
          />
        </TabsContent>
      </Tabs>

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
      fallback={<p className="text-sm text-muted-foreground">Loading…</p>}
    >
      <EngagementGate />
    </Suspense>
  );
}
