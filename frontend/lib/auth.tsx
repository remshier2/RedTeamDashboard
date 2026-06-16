"use client";

// Auth abstraction with two implementations chosen by a build-time constant
// (ENTRA_ENABLED) so hooks are never called conditionally:
//   - MsalAuthProvider: real Entra SSO (sign-in/out, identity from MSAL).
//   - DevAuthProvider:   a fixed dev identity, no sign-in (local dev).
// Components use useAuth() regardless of which is active.
//
// We drive @azure/msal-browser directly (no @azure/msal-react) — its needs
// here are small and msal-react's react peer range lags React 19.

import { createContext, useContext, useEffect, useState } from "react";
import { DEV_USER, ENTRA, ENTRA_ENABLED } from "@/lib/config";
import { activeAccount, ensureMsalReady, msalInstance } from "@/lib/msal";

export interface Identity {
  name: string;
  username: string;
}

interface AuthValue {
  ready: boolean;
  enabled: boolean;
  identity: Identity | null;
  signIn: () => void;
  signOut: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}

// ── Entra (MSAL) implementation ──────────────────────────────────────────

function MsalAuthProvider({ children }: { children: React.ReactNode }) {
  // msalInstance is non-null here (ENTRA_ENABLED gates the export selection).
  const instance = msalInstance!;
  const [ready, setReady] = useState(false);
  const [identity, setIdentity] = useState<Identity | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      await ensureMsalReady();
      // Capture the account returned from a login redirect, if any.
      const resp = await instance.handleRedirectPromise();
      if (resp?.account) {
        instance.setActiveAccount(resp.account);
      } else if (!instance.getActiveAccount() && instance.getAllAccounts()[0]) {
        instance.setActiveAccount(instance.getAllAccounts()[0]);
      }
      if (cancelled) return;
      const account = activeAccount();
      setIdentity(
        account
          ? { name: account.name ?? account.username, username: account.username }
          : null,
      );
      setReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [instance]);

  const value: AuthValue = {
    ready,
    enabled: true,
    identity,
    signIn: () => {
      void instance.loginRedirect({ scopes: [ENTRA.apiScope] });
    },
    signOut: () => {
      void instance.logoutRedirect();
    },
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// ── Dev implementation (no tenant) ───────────────────────────────────────

function DevAuthProvider({ children }: { children: React.ReactNode }) {
  const value: AuthValue = {
    ready: true,
    enabled: false,
    identity: { name: DEV_USER, username: DEV_USER },
    signIn: () => {},
    signOut: () => {},
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export const AuthProvider = ENTRA_ENABLED ? MsalAuthProvider : DevAuthProvider;
