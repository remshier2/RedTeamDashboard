"use client";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";

// Header-right identity slot. Shows the signed-in analyst + sign-out under
// Entra; in dev mode it shows the dev identity with a muted "(dev)" tag.
export function IdentityMenu() {
  const { enabled, identity, signOut } = useAuth();
  if (!identity) return null;
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-muted-foreground">
        {identity.name}
        {!enabled && (
          <span className="ml-1 text-xs text-muted-foreground/60">(dev)</span>
        )}
      </span>
      {enabled && (
        <Button variant="outline" size="sm" onClick={signOut}>
          Sign out
        </Button>
      )}
    </div>
  );
}
