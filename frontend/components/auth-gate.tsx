"use client";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth";

// Blocks the app until an analyst is signed in (Entra mode). In dev mode
// there's no real sign-in, so it passes straight through.
export function AuthGate({ children }: { children: React.ReactNode }) {
  const { ready, enabled, identity, signIn } = useAuth();

  if (!enabled) return <>{children}</>;

  if (!ready) {
    return (
      <p className="container py-10 text-sm text-muted-foreground">Loading…</p>
    );
  }

  if (!identity) {
    return (
      <div className="container flex min-h-[60vh] flex-col items-center justify-center gap-4 text-center">
        <span className="h-4 w-1.5 rounded-full bg-critical" />
        <h1 className="text-xl font-semibold tracking-tight">
          Red Team Dashboard
        </h1>
        <p className="max-w-sm text-sm text-muted-foreground">
          Sign in with your organization account to access engagements.
        </p>
        <Button onClick={signIn}>Sign in</Button>
      </div>
    );
  }

  return <>{children}</>;
}
