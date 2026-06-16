// MSAL singleton + token acquisition usable outside React (api.ts/events.ts
// need a token without hooks). The instance only exists when Entra is
// configured; in dev mode every export is a harmless no-op.

import {
  InteractionRequiredAuthError,
  type AccountInfo,
  PublicClientApplication,
} from "@azure/msal-browser";
import { ENTRA, ENTRA_ENABLED } from "@/lib/config";

export const msalInstance: PublicClientApplication | null = ENTRA_ENABLED
  ? new PublicClientApplication({
      auth: {
        clientId: ENTRA.clientId,
        authority: `https://login.microsoftonline.com/${ENTRA.tenantId}`,
        redirectUri:
          typeof window !== "undefined" ? window.location.origin : undefined,
      },
      cache: { cacheLocation: "localStorage" },
    })
  : null;

const SCOPES = ENTRA.apiScope ? [ENTRA.apiScope] : [];

// msal-browser v3 requires initialize() before any other call; memoize it.
let initPromise: Promise<void> | null = null;
export function ensureMsalReady(): Promise<void> {
  if (!msalInstance) return Promise.resolve();
  if (!initPromise) initPromise = msalInstance.initialize();
  return initPromise;
}

export function activeAccount(): AccountInfo | null {
  if (!msalInstance) return null;
  return (
    msalInstance.getActiveAccount() ?? msalInstance.getAllAccounts()[0] ?? null
  );
}

// Acquire an API access token. Silent first; on interaction-required, kick off
// a redirect (which navigates away, so we return null). Returns null when
// Entra is disabled.
export async function getAccessToken(): Promise<string | null> {
  if (!msalInstance) return null;
  await ensureMsalReady();
  const account = activeAccount();
  if (!account) {
    await msalInstance.loginRedirect({ scopes: SCOPES });
    return null;
  }
  try {
    const result = await msalInstance.acquireTokenSilent({
      account,
      scopes: SCOPES,
    });
    return result.accessToken;
  } catch (err) {
    if (err instanceof InteractionRequiredAuthError) {
      await msalInstance.acquireTokenRedirect({ scopes: SCOPES });
      return null;
    }
    throw err;
  }
}
