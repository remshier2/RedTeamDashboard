"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { decideApproval } from "@/lib/api";

export interface PendingApproval {
  approval_id: string;
  thread_id: string;
  tool: string;
  args: Record<string, unknown>;
  risk: string;
  scope: Record<string, unknown>;
  tool_call_id: string;
}

export function ApprovalsModal({
  pending,
  onResolved,
}: {
  pending: PendingApproval | null;
  onResolved: (approvalId: string) => void;
}) {
  const [argsJson, setArgsJson] = useState("");
  const [reason, setReason] = useState("");
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (pending) {
      setArgsJson(JSON.stringify(pending.args, null, 2));
      setReason("");
      setRemember(false);
      setError(null);
    }
  }, [pending]);

  if (!pending) return null;

  const decide = async (kind: "approve" | "edit" | "deny") => {
    setBusy(true);
    setError(null);
    try {
      if (kind === "approve") {
        await decideApproval(pending.approval_id, {
          approved: true,
          remember_for_session: remember,
        });
      } else if (kind === "edit") {
        let edited: Record<string, unknown>;
        try {
          edited = JSON.parse(argsJson);
        } catch {
          setError("Edited args must be valid JSON");
          setBusy(false);
          return;
        }
        await decideApproval(pending.approval_id, {
          approved: true,
          edited_args: edited,
          remember_for_session: remember,
        });
      } else {
        await decideApproval(pending.approval_id, {
          approved: false,
          reason: reason.trim() || "denied by operator",
        });
      }
      onResolved(pending.approval_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onResolved(pending.approval_id)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Approval required: {pending.tool}</DialogTitle>
          <DialogDescription>
            Risk: <span className="font-mono">{pending.risk}</span> · thread{" "}
            <span className="font-mono">{pending.thread_id.slice(0, 8)}…</span>
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label>Tool args (edit to approve with changes)</Label>
            <Textarea
              value={argsJson}
              onChange={(event) => setArgsJson(event.target.value)}
              rows={4}
              className="font-mono text-xs"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="reason">Denial reason (if denying)</Label>
            <Textarea
              id="reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={2}
              placeholder="optional"
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
              className="h-4 w-4"
            />
            Remember for this session — auto-approve future{" "}
            <span className="font-mono">{pending.tool}</span> calls (in-scope
            only) until revoked
          </label>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        <DialogFooter className="gap-2">
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => decide("deny")}
          >
            Deny
          </Button>
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => decide("edit")}
          >
            Approve with edits
          </Button>
          <Button disabled={busy} onClick={() => decide("approve")}>
            Approve
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
