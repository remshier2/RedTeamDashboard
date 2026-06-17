"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Upload } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { parseScope } from "@/lib/api";
import type { ScopeImportPreview } from "@/lib/types";

const PREVIEW_DEBOUNCE_MS = 300;
const ACCEPT_TYPES = ".txt,.csv,text/plain,text/csv";

// Bulk-import drop-in. Parent decides what to do on commit:
//   /new wizard      -> stash parsed rows, commit alongside engagement create
//   scope editor     -> POST /engagements/{slug}/scope/import + refresh list

export function ScopeImporter({
  onCommit,
  busyLabel = "Importing…",
}: {
  onCommit: (text: string, preview: ScopeImportPreview) => Promise<void> | void;
  busyLabel?: string;
}) {
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<ScopeImportPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [committing, setCommitting] = useState(false);
  const [commitError, setCommitError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Debounced live preview against the backend parser. Keeps client/server
  // classification consistent — one source of truth.
  useEffect(() => {
    if (!text.trim()) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    const handle = window.setTimeout(async () => {
      setPreviewing(true);
      setPreviewError(null);
      try {
        setPreview(await parseScope(text));
      } catch (err) {
        setPreviewError(err instanceof Error ? err.message : String(err));
        setPreview(null);
      } finally {
        setPreviewing(false);
      }
    }, PREVIEW_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [text]);

  const onFileChosen = useCallback(
    async (event: React.ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      if (!file) return;
      const body = await file.text();
      setText(body);
      event.target.value = ""; // allow re-uploading same file
    },
    [],
  );

  const onCommitClick = async () => {
    if (!preview || preview.would_create === 0) return;
    setCommitting(true);
    setCommitError(null);
    try {
      await onCommit(text, preview);
      setText("");
      setPreview(null);
    } catch (err) {
      setCommitError(err instanceof Error ? err.message : String(err));
    } finally {
      setCommitting(false);
    }
  };

  return (
    <div className="space-y-3 rounded-md border border-dashed border-border p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">Bulk import</p>
          <p className="text-xs text-muted-foreground">
            Paste a list or upload a .txt / .csv. One target per line; commas
            inside a line also split. Lines starting with{" "}
            <code className="font-mono">#</code> are comments,{" "}
            <code className="font-mono">!</code> marks an exclusion.
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => fileRef.current?.click()}
        >
          <Upload className="mr-1.5 h-3.5 w-3.5" />
          File
        </Button>
        <input
          ref={fileRef}
          type="file"
          accept={ACCEPT_TYPES}
          className="hidden"
          onChange={onFileChosen}
        />
      </div>

      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={6}
        placeholder={
          "acme.test\n10.0.0.0/24\n!10.0.0.5\nhttps://portal.acme.test"
        }
        className="font-mono text-xs"
      />

      {previewError && (
        <p className="text-xs text-critical">Preview failed: {previewError}</p>
      )}

      {preview && (
        <div className="space-y-2 text-xs">
          <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
            <span>
              <span className="text-foreground">{preview.would_create}</span>{" "}
              would import
            </span>
            {preview.errors.length > 0 && (
              <span className="text-critical">
                · {preview.errors.length} unparseable
              </span>
            )}
            {previewing && <span>· refreshing…</span>}
          </div>

          {preview.preview.length > 0 && (
            <ul className="max-h-40 overflow-auto rounded border border-border bg-background">
              {preview.preview.map((row) => (
                <li
                  key={`${row.line}-${row.value}`}
                  className="flex items-center justify-between border-b border-border/60 px-2 py-1 last:border-0"
                >
                  <span className="flex items-center gap-2">
                    <Badge
                      variant={row.is_exclusion ? "destructive" : "secondary"}
                      className="font-mono text-[10px]"
                    >
                      {row.kind}
                      {row.is_exclusion ? " · exclude" : ""}
                    </Badge>
                    <span className="font-mono">{row.value}</span>
                  </span>
                  <span className="text-muted-foreground">L{row.line}</span>
                </li>
              ))}
            </ul>
          )}

          {preview.errors.length > 0 && (
            <ul className="max-h-32 overflow-auto rounded border border-critical/30 bg-critical/5 p-1.5 font-mono text-[10px]">
              {preview.errors.map((err, i) => (
                <li key={i} className="px-1.5 py-0.5 text-critical">
                  L{err.line}: {err.raw || "(blank)"} — {err.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {commitError && (
        <p className="text-xs text-critical">{commitError}</p>
      )}

      <div className="flex justify-end">
        <Button
          type="button"
          size="sm"
          disabled={
            committing ||
            previewing ||
            !preview ||
            preview.would_create === 0
          }
          onClick={onCommitClick}
        >
          {committing
            ? busyLabel
            : preview
              ? `Import ${preview.would_create}`
              : "Import"}
        </Button>
      </div>
    </div>
  );
}
