// Build-time configuration. NEXT_PUBLIC_* values are inlined by Next at build
// time (the SWA build step / `npm run build` must have them set). See
// frontend/.env.example.
//
// Phase 7 retires the multi-"source" model: the viewer talks to ONE backend
// (API_BASE_URL) and identifies the analyst via Entra SSO. When the Entra
// vars are absent (local dev) we fall back to a dev identity so the app still
// works against a local backend without a tenant.

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export const ENTRA = {
  tenantId: process.env.NEXT_PUBLIC_ENTRA_TENANT_ID ?? "",
  clientId: process.env.NEXT_PUBLIC_ENTRA_CLIENT_ID ?? "",
  apiScope: process.env.NEXT_PUBLIC_ENTRA_API_SCOPE ?? "",
};

// Real SSO only when all three are present; otherwise dev-identity fallback.
export const ENTRA_ENABLED = Boolean(
  ENTRA.tenantId && ENTRA.clientId && ENTRA.apiScope,
);

// Dev-mode identity sent as X-User-Id when Entra is disabled.
export const DEV_USER = process.env.NEXT_PUBLIC_DEV_USER ?? "analyst@localhost";
