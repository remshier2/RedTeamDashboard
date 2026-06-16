"use client";

import {
  DollarSign,
  FileText,
  ListChecks,
  Network,
  Target,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

export type EngagementView =
  | "findings"
  | "entities"
  | "report"
  | "costs"
  | "scope";

const ITEMS: { view: EngagementView; label: string; Icon: LucideIcon }[] = [
  { view: "findings", label: "Findings", Icon: ListChecks },
  { view: "entities", label: "Entities", Icon: Network },
  { view: "report", label: "Report", Icon: FileText },
  { view: "costs", label: "Costs", Icon: DollarSign },
  { view: "scope", label: "Scope", Icon: Target },
];

// Left rail for the engagement workspace. Selecting an item swaps the whole
// content pane (page-level), per the CHARTER's left-nav direction. The active
// item carries the single ember accent.
export function EngagementNav({
  active,
  onSelect,
}: {
  active: EngagementView;
  onSelect: (view: EngagementView) => void;
}) {
  return (
    <nav className="w-44 shrink-0">
      <ul className="sticky top-20 space-y-1">
        {ITEMS.map(({ view, label, Icon }) => {
          const selected = active === view;
          return (
            <li key={view}>
              <button
                type="button"
                onClick={() => onSelect(view)}
                aria-current={selected ? "page" : undefined}
                className={cn(
                  "flex w-full items-center gap-2.5 rounded-md border-l-2 px-3 py-2 text-sm transition-colors",
                  selected
                    ? "border-critical bg-secondary/60 text-foreground"
                    : "border-transparent text-muted-foreground hover:bg-secondary/40 hover:text-foreground",
                )}
              >
                <Icon className="h-4 w-4" />
                {label}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
