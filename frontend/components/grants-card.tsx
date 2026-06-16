"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { listAuthorizations, revokeAuthorization } from "@/lib/api";
import type { Authorization } from "@/lib/types";

interface GrantsCardProps {
  engagementId: string;
  // Bumping this triggers a refetch — parent does so after a "remember"
  // approval (a new grant may have appeared) and after this card revokes one.
  refreshKey: number;
  canRevoke: boolean;
}

export function GrantsCard({
  engagementId,
  refreshKey,
  canRevoke,
}: GrantsCardProps) {
  const [grants, setGrants] = useState<Authorization[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await listAuthorizations(engagementId, true);
      setGrants(rows);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [engagementId]);

  useEffect(() => {
    reload();
  }, [reload, refreshKey]);

  const onRevoke = async (grant: Authorization) => {
    if (
      !window.confirm(
        `Revoke session grant for ${grant.tool_name}? Future calls will prompt for approval again.`,
      )
    ) {
      return;
    }
    setBusyId(grant.id);
    try {
      await revokeAuthorization(grant.id);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Session grants</CardTitle>
        <CardDescription>
          Per-tool standing approvals. While active, in-scope calls to that tool
          auto-run instead of prompting.
          {canRevoke ? " Revoke to require approval again." : null}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {error && (
          <p className="mb-2 text-sm text-destructive">{error}</p>
        )}
        {loading ? (
          <p className="text-sm text-muted-foreground">Loading grants…</p>
        ) : grants.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No active session grants. Approve an active tool with “Remember for
            this session” to create one.
          </p>
        ) : (
          <ul className="space-y-2">
            {grants.map((grant) => (
              <li
                key={grant.id}
                className="flex items-center justify-between rounded border bg-muted/40 px-3 py-2"
              >
                <div className="text-sm">
                  <div className="font-mono">{grant.tool_name}</div>
                  <div className="text-xs text-muted-foreground">
                    granted {new Date(grant.created_at).toLocaleString()}
                  </div>
                </div>
                {canRevoke && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={busyId === grant.id}
                    onClick={() => onRevoke(grant)}
                  >
                    {busyId === grant.id ? "Revoking…" : "Revoke"}
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
