"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Upload } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { importFindings, importFindingsNessus } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  Finding,
  FindingImport,
  FindingPhase,
  NessusImportResult,
  Severity,
} from "@/lib/types";

const SEVERITIES: Severity[] = ["info", "low", "medium", "high", "critical"];
const PHASES: FindingPhase[] = ["osint", "vuln_scan", "exploit", "phishing", "general"];

const SEVERITY_CLASS: Record<Severity, string> = {
  critical: "border-critical/50 bg-critical/15 text-critical",
  high: "border-zinc-500/40 text-zinc-100",
  medium: "border-zinc-600/40 text-zinc-300",
  low: "border-zinc-700/40 text-zinc-400",
  info: "border-zinc-800 text-zinc-500",
};

// ── CSV parsing ──────────────────────────────────────────────────────────────

function parseCSVLine(line: string): string[] {
  const result: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (inQuotes && line[i + 1] === '"') {
        current += '"';
        i++;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (ch === "," && !inQuotes) {
      result.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  result.push(current.trim());
  return result;
}

function parseFindingsCSV(raw: string): {
  rows: FindingImport[];
  errors: string[];
} {
  const lines = raw
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#"));

  if (lines.length < 2)
    return { rows: [], errors: ["Need a header row plus at least one data row."] };

  const headers = parseCSVLine(lines[0]).map((h) => h.toLowerCase());
  if (!headers.includes("title"))
    return { rows: [], errors: ['Missing required column: "title"'] };

  const rows: FindingImport[] = [];
  const errors: string[] = [];

  for (let i = 1; i < lines.length; i++) {
    const values = parseCSVLine(lines[i]);
    const col = (name: string) => {
      const idx = headers.indexOf(name);
      return idx >= 0 ? (values[idx] ?? "").trim() : "";
    };

    const title = col("title");
    if (!title) {
      errors.push(`Row ${i + 1}: missing title — skipped`);
      continue;
    }

    const rawSev = col("severity").toLowerCase() as Severity;
    const rawPhase = col("phase").toLowerCase() as FindingPhase;

    rows.push({
      title,
      severity: SEVERITIES.includes(rawSev) ? rawSev : "info",
      phase: PHASES.includes(rawPhase) ? rawPhase : "general",
      summary: col("summary") || undefined,
      target: col("target") || undefined,
      source_tool: col("source_tool") || "import",
    });
  }

  return { rows, errors };
}

function parseFindingsJSON(raw: string): {
  rows: FindingImport[];
  errors: string[];
} {
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed))
      return { rows: [], errors: ["Expected a JSON array of finding objects."] };

    const rows: FindingImport[] = [];
    const errors: string[] = [];

    parsed.forEach((item: unknown, i: number) => {
      if (typeof item !== "object" || item === null || !("title" in item)) {
        errors.push(`Item ${i + 1}: missing "title" — skipped`);
        return;
      }
      const obj = item as Record<string, unknown>;
      rows.push({
        title: String(obj.title ?? ""),
        severity: SEVERITIES.includes(obj.severity as Severity)
          ? (obj.severity as Severity)
          : "info",
        phase: PHASES.includes(obj.phase as FindingPhase)
          ? (obj.phase as FindingPhase)
          : "general",
        summary: obj.summary ? String(obj.summary) : undefined,
        target: obj.target ? String(obj.target) : undefined,
        source_tool: obj.source_tool ? String(obj.source_tool) : "import",
        details: (obj.details as Record<string, unknown>) ?? {},
      });
    });

    return { rows, errors };
  } catch (err) {
    return {
      rows: [],
      errors: [`JSON parse error: ${err instanceof Error ? err.message : String(err)}`],
    };
  }
}

// ── component ────────────────────────────────────────────────────────────────

type Mode = "csv" | "json" | "nessus";

const CSV_PLACEHOLDER = `title,severity,phase,summary,target,source_tool
SQL injection in login form,high,vuln_scan,Bypasses authentication,https://api.example.com/login,manual
Reflected XSS in search,medium,vuln_scan,,https://app.example.com/search,burp`;

const JSON_PLACEHOLDER = `[
  {
    "title": "SQL injection in login form",
    "severity": "high",
    "phase": "vuln_scan",
    "summary": "Bypasses authentication",
    "target": "https://api.example.com/login",
    "source_tool": "manual"
  }
]`;

export function FindingImporter({
  slug,
  onImported,
}: {
  slug: string;
  onImported: (findings: Finding[]) => void;
}) {
  const [mode, setMode] = useState<Mode>("csv");
  const [text, setText] = useState("");
  const [parsed, setParsed] = useState<{
    rows: FindingImport[];
    errors: string[];
  } | null>(null);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  // Nessus-mode state. Kept separate from the text-paste flow because
  // .nessus files are routinely 1-10MB and we don't pre-parse client-side
  // — the result panel reports the post-import counts from the server.
  const [nessusFile, setNessusFile] = useState<File | null>(null);
  const [includeInfo, setIncludeInfo] = useState(false);
  const [nessusResult, setNessusResult] = useState<NessusImportResult | null>(
    null,
  );

  useEffect(() => {
    if (mode === "nessus") {
      setParsed(null);
      return;
    }
    if (!text.trim()) {
      setParsed(null);
      return;
    }
    setParsed(mode === "csv" ? parseFindingsCSV(text) : parseFindingsJSON(text));
  }, [text, mode]);

  const onFileChosen = useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      // Auto-detect by extension. .nessus → file-only Nessus mode; other
      // file types feed the existing text-paste flow.
      if (file.name.endsWith(".nessus")) {
        setMode("nessus");
        setNessusFile(file);
        setNessusResult(null);
        setText("");
      } else {
        setText(await file.text());
        if (file.name.endsWith(".json")) setMode("json");
        else setMode("csv");
      }
      e.target.value = "";
    },
    [],
  );

  const onImport = async () => {
    if (mode === "nessus") {
      if (!nessusFile) return;
      setImporting(true);
      setImportError(null);
      try {
        const result = await importFindingsNessus(slug, nessusFile, includeInfo);
        onImported(result.imported);
        setNessusResult(result);
        setNessusFile(null);
      } catch (err) {
        setImportError(err instanceof Error ? err.message : String(err));
      } finally {
        setImporting(false);
      }
      return;
    }
    if (!parsed || parsed.rows.length === 0) return;
    setImporting(true);
    setImportError(null);
    try {
      const created = await importFindings(slug, parsed.rows);
      onImported(created);
      setText("");
      setParsed(null);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : String(err));
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="space-y-3 rounded-md border border-dashed border-border p-3">
      {/* Header + mode toggle */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">Import findings</p>
          <p className="text-xs text-muted-foreground">
            Paste or upload CSV / JSON, or upload a Nessus .nessus XML
            export. All imports land as <em>pending validation</em> for
            analyst review.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {(["csv", "json", "nessus"] as Mode[]).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => {
                setMode(m);
                setText("");
                setNessusFile(null);
                setNessusResult(null);
                setImportError(null);
              }}
              className={cn(
                "rounded border px-2 py-0.5 text-xs uppercase tracking-wide transition-colors",
                mode === m
                  ? "border-foreground/30 bg-secondary text-foreground"
                  : "border-border text-muted-foreground hover:text-foreground",
              )}
            >
              {m}
            </button>
          ))}
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="ml-1"
            onClick={() => fileRef.current?.click()}
          >
            <Upload className="mr-1.5 h-3.5 w-3.5" />
            File
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept=".csv,.json,.txt,.nessus,text/csv,application/json,text/plain,text/xml,application/xml"
            className="hidden"
            onChange={onFileChosen}
          />
        </div>
      </div>

      {/* CSV column hint */}
      {mode === "csv" && !text && (
        <p className="text-[11px] text-muted-foreground/70">
          Columns:{" "}
          <code className="font-mono">title</code> (required),{" "}
          <code className="font-mono">severity</code>,{" "}
          <code className="font-mono">phase</code>,{" "}
          <code className="font-mono">summary</code>,{" "}
          <code className="font-mono">target</code>,{" "}
          <code className="font-mono">source_tool</code>
        </p>
      )}

      {/* CSV/JSON paste-or-loaded text body */}
      {mode !== "nessus" && (
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          placeholder={mode === "csv" ? CSV_PLACEHOLDER : JSON_PLACEHOLDER}
          className="font-mono text-xs"
        />
      )}

      {/* Nessus: file-only upload + include_info toggle */}
      {mode === "nessus" && (
        <div className="space-y-2 rounded border border-border bg-background p-2 text-xs">
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground">File:</span>
            <span className="truncate font-mono">
              {nessusFile?.name ?? (
                <span className="text-muted-foreground/70">
                  none selected — click the File button above
                </span>
              )}
            </span>
            {nessusFile && (
              <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground">
                {(nessusFile.size / 1024).toFixed(1)} KB
              </span>
            )}
          </div>
          <label className="flex cursor-pointer items-center gap-2 text-muted-foreground">
            <input
              type="checkbox"
              checked={includeInfo}
              onChange={(e) => setIncludeInfo(e.target.checked)}
              className="accent-foreground"
            />
            <span>
              Include <span className="font-mono">severity=Info</span> findings
            </span>
          </label>
          <p className="text-[11px] text-muted-foreground/70">
            Out-of-scope hosts are dropped silently. Counts are shown below
            after import.
          </p>
        </div>
      )}

      {/* Parse preview */}
      {parsed && (
        <div className="space-y-2 text-xs">
          <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
            <span>
              <span className="text-foreground">{parsed.rows.length}</span> finding
              {parsed.rows.length !== 1 ? "s" : ""} ready to import
            </span>
            {parsed.errors.length > 0 && (
              <span className="text-critical">
                · {parsed.errors.length} row{parsed.errors.length !== 1 ? "s" : ""} skipped
              </span>
            )}
          </div>

          {parsed.rows.length > 0 && (
            <ul className="max-h-40 overflow-auto rounded border border-border bg-background">
              {parsed.rows.map((row, i) => (
                <li
                  key={i}
                  className="flex items-center gap-2 border-b border-border/60 px-2 py-1 last:border-0"
                >
                  <Badge
                    variant="outline"
                    className={cn("shrink-0 font-mono text-[10px]", SEVERITY_CLASS[row.severity ?? "info"])}
                  >
                    {row.severity}
                  </Badge>
                  <span className="truncate">{row.title}</span>
                  <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground">
                    {row.phase}
                  </span>
                </li>
              ))}
            </ul>
          )}

          {parsed.errors.length > 0 && (
            <ul className="max-h-24 overflow-auto rounded border border-critical/30 bg-critical/5 p-1.5 font-mono text-[10px]">
              {parsed.errors.map((err, i) => (
                <li key={i} className="px-1 py-0.5 text-critical">
                  {err}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Nessus post-import result panel (server-side counts) */}
      {mode === "nessus" && nessusResult && (
        <div className="space-y-1 rounded border border-border bg-background p-2 text-xs">
          <div className="font-medium">
            Imported{" "}
            <span className="font-mono">{nessusResult.imported.length}</span> of{" "}
            <span className="font-mono">{nessusResult.total_items}</span>{" "}
            ReportItems
          </div>
          {(nessusResult.skipped_info > 0 ||
            nessusResult.skipped_out_of_scope > 0) && (
            <div className="text-muted-foreground">
              Skipped:{" "}
              {nessusResult.skipped_info > 0 && (
                <span>
                  <span className="font-mono">{nessusResult.skipped_info}</span>{" "}
                  info
                </span>
              )}
              {nessusResult.skipped_info > 0 &&
                nessusResult.skipped_out_of_scope > 0 && <span> · </span>}
              {nessusResult.skipped_out_of_scope > 0 && (
                <span>
                  <span className="font-mono">
                    {nessusResult.skipped_out_of_scope}
                  </span>{" "}
                  out-of-scope
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {importError && (
        <p className="text-xs text-critical">{importError}</p>
      )}

      <div className="flex justify-end">
        <Button
          type="button"
          size="sm"
          disabled={
            importing ||
            (mode === "nessus"
              ? !nessusFile
              : !parsed || parsed.rows.length === 0)
          }
          onClick={onImport}
        >
          {importing
            ? "Importing…"
            : mode === "nessus"
              ? nessusFile
                ? "Import Nessus"
                : "Import"
              : parsed && parsed.rows.length > 0
                ? `Import ${parsed.rows.length}`
                : "Import"}
        </Button>
      </div>
    </div>
  );
}
