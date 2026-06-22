"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp } from "lucide-react";
import { getEngagementCosts } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { AgentCost, ModelCost, CostRollup } from "@/lib/types";

const AGENT_LABEL: Record<string, string> = {
  strategic: "Strategic",
  tactical: "Tactical",
};

function formatNumber(n: number): string {
  return new Intl.NumberFormat("en-US").format(n);
}

function formatCurrency(usd: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(usd);
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) {
    return `${(n / 1_000_000).toFixed(1)}M`;
  }
  if (n >= 1_000) {
    return `${(n / 1_000).toFixed(1)}K`;
  }
  return formatNumber(n);
}

interface CostBucketProps {
  label: string;
  executions: number;
  tokensIn: number;
  tokensOut: number;
  costUsd: number;
  className?: string;
}

function CostBucketCard({
  label,
  executions,
  tokensIn,
  tokensOut,
  costUsd,
  className,
}: CostBucketProps) {
  return (
    <div className={cn("rounded-lg border border-border p-4", className)}>
      <h3 className="text-sm font-medium text-muted-foreground">{label}</h3>
      <div className="mt-3 grid grid-cols-2 gap-4">
        <div>
          <p className="text-xs text-muted-foreground">Executions</p>
          <p className="text-lg font-semibold tabular-nums">{formatNumber(executions)}</p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">Cost (USD)</p>
          <p className="text-lg font-semibold tabular-nums">{formatCurrency(costUsd)}</p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">Tokens In</p>
          <p className="text-sm font-mono tabular-nums">{formatTokens(tokensIn)}</p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">Tokens Out</p>
          <p className="text-sm font-mono tabular-nums">{formatTokens(tokensOut)}</p>
        </div>
      </div>
    </div>
  );
}

interface ExpandableSectionProps {
  title: string;
  count: number;
  children: React.ReactNode;
}

function ExpandableSection({ title, count, children }: ExpandableSectionProps) {
  const [expanded, setExpanded] = useState(false);

  if (count === 0) return null;

  return (
    <div className="rounded-lg border border-border">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-2.5 text-sm font-medium hover:bg-secondary/30"
      >
        <span>{title}</span>
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">{count}</span>
          {expanded ? (
            <ChevronUp className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          )}
        </div>
      </button>
      {expanded && <div className="border-t border-border p-2">{children}</div>}
    </div>
  );
}

export function CostsView({ slug }: { slug: string }) {
  const [data, setData] = useState<CostRollup | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    getEngagementCosts(slug)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [slug]);

  if (error) return <p className="text-sm text-critical">{error}</p>;
  if (loading) return <p className="text-sm text-muted-foreground">Loading costs…</p>;
  if (!data) return null;

  const hasUnpriced = data.unpriced_models.length > 0;

  return (
    <div className="space-y-6">
      {/* Total card */}
      <CostBucketCard
        label="Total LLM Spend"
        executions={data.total.executions}
        tokensIn={data.total.tokens_in}
        tokensOut={data.total.tokens_out}
        costUsd={data.total.cost_usd}
        className="border-critical/30 bg-critical/5"
      />

      {data.total.executions === 0 && (
        <p className="text-sm text-muted-foreground">
          No agent executions recorded yet. Costs will accumulate as the Strategic
          and Tactical orchestrators run.
        </p>
      )}

      {/* Unpriced warning */}
      {hasUnpriced && (
        <div className="flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3">
          <AlertTriangle className="h-5 w-5 shrink-0 text-amber-500" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-amber-100">
              Unpriced model{data.unpriced_models.length > 1 ? "s" : ""}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              The following models lack pricing entries in the rate table. Their
              tokens are counted but cost is reported as $0:
            </p>
            <ul className="mt-1.5 flex flex-wrap gap-1">
              {data.unpriced_models.map((m) => (
                <li
                  key={m}
                  className="rounded-md border border-amber-500/30 bg-amber-500/5 px-2 py-0.5 text-xs font-mono text-amber-200"
                >
                  {m}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* By agent breakdown */}
      <ExpandableSection
        title="By Agent"
        count={data.by_agent.length}
      >
        <div className="space-y-2">
          {data.by_agent.map((agent: AgentCost) => (
            <CostBucketCard
              key={agent.agent}
              label={AGENT_LABEL[agent.agent] ?? agent.agent}
              executions={agent.executions}
              tokensIn={agent.tokens_in}
              tokensOut={agent.tokens_out}
              costUsd={agent.cost_usd}
              className="border-border/60"
            />
          ))}
        </div>
      </ExpandableSection>

      {/* By model breakdown */}
      <ExpandableSection
        title="By Model"
        count={data.by_model.length}
      >
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th className="px-3 py-2">Provider</th>
                <th className="px-3 py-2">Model</th>
                <th className="px-3 py-2 w-20">Executions</th>
                <th className="px-3 py-2 w-24">Tokens In</th>
                <th className="px-3 py-2 w-24">Tokens Out</th>
                <th className="px-3 py-2 w-24">Cost (USD)</th>
                <th className="px-3 py-2 w-16">Priced</th>
              </tr>
            </thead>
            <tbody>
              {data.by_model.map((model: ModelCost, idx: number) => (
                <tr
                  key={idx}
                  className="border-b border-border/60 last:border-0"
                >
                  <td className="px-3 py-2.5 text-xs text-muted-foreground">
                    {model.provider ?? "—"}
                  </td>
                  <td className="px-3 py-2.5 font-mono text-xs">
                    {model.model ?? "—"}
                  </td>
                  <td className="px-3 py-2.5 tabular-nums text-muted-foreground">
                    {formatNumber(model.executions)}
                  </td>
                  <td className="px-3 py-2.5 tabular-nums text-muted-foreground">
                    {formatTokens(model.tokens_in)}
                  </td>
                  <td className="px-3 py-2.5 tabular-nums text-muted-foreground">
                    {formatTokens(model.tokens_out)}
                  </td>
                  <td className="px-3 py-2.5 tabular-nums font-medium">
                    {formatCurrency(model.cost_usd)}
                  </td>
                  <td className="px-3 py-2.5">
                    <span
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-xs",
                        model.priced
                          ? "border-success/50 bg-success/15 text-success"
                          : "border-amber-500/50 bg-amber-500/15 text-amber-500",
                      )}
                    >
                      {model.priced ? "Yes" : "No"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </ExpandableSection>

      {/* Footnote about local providers */}
      <p className="text-xs text-muted-foreground">
        Local providers (e.g. Ollama) are reported at $0 cost regardless of
        token count. Models without a pricing entry are flagged as unpriced above.
      </p>
    </div>
  );
}
