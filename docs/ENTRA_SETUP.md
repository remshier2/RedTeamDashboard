<!--
RedTeamDashboard — Defensive Security Operations and Governance Platform

This documentation describes a platform for managing authorized security engagements.
All work described is conducted with explicit approval and scope boundaries.

Charter:
- Agents perform enumeration and scanning only
- Validation/proof-of-concept work is analyst-only
- All actions are approval-gated and audit-logged

Terminology Note: "exploit" in this context refers to validation/proof-of-concept
work conducted by analysts during authorized engagements, not unauthorized intrusion.
-->

# Entra ID setup — analyst SSO for the Red Team Dashboard

Phase 7 replaces the API-key/source model in the browser with **per-analyst
Microsoft Entra ID sign-in**. Analysts sign in with their org account; the
viewer (MSAL.js) gets an access token and sends it to the backend, which
validates it and resolves the analyst to a `User` by the token's `oid`.

> The **CLI keeps using API keys** — this only changes the browser path.

## Topology — one app registration

We use a **single first-party app registration** that is both:
- the **SPA client** analysts sign into, and
- the **API** it calls (it exposes one scope, `access_as_user`).

The SPA requests an access token for `api://<client-id>/access_as_user`; the
token's audience is the same app, so no cross-app permission grant or admin
consent is needed. (If you ever need third parties to call the API, split this
into two apps later.)

```
Analyst ──sign in──▶ SPA (viewer)  ──Bearer access token──▶ Backend API
                     └────────── same Entra app (aud = api://<client-id>) ──────────┘
```

This works on the **SWA Free SKU** — MSAL is app-level auth, not SWA's
built-in (Standard-only) auth block.

## Prerequisites

- An Entra tenant you control (`az account show --query tenantId`).
- Rights to register apps: **Application Administrator**, or the tenant has
  *Users → User settings → "Users can register applications" = Yes*.
- The deployed **viewer URL** (the SWA hostname) for the redirect URI.

## Option A — automated (recommended)

```bash
az login
./infra/azure-kit/scripts/setup-entra.sh \
    --env prod \
    --viewer-url https://<your-viewer>.azurestaticapps.net \
    --dev-origin http://localhost:3001      # optional, for local dev
```

It creates the app, sets `api://<appId>` as the identifier URI, exposes the
`access_as_user` scope, registers the SPA redirect URIs, creates the service
principal, and prints the exact env values to set (below). Re-runnable — it
reuses an app of the same display name and won't duplicate the scope.

## Option B — Azure Portal click-through (authoritative fallback)

If the script hits a tenant policy, do it by hand:

1. **Entra admin center → App registrations → New registration.**
   - Name: `rtd-prod-viewer` · Supported accounts: *this org only* · skip
     redirect for now → **Register**. Note the **Application (client) ID** and
     **Directory (tenant) ID**.
2. **Expose an API →** set Application ID URI to `api://<client-id>` (default)
   → **Add a scope** named `access_as_user`, *Admins and users* can consent,
   fill the display/description fields → **Add scope**.
3. **Authentication → Add a platform → Single-page application.** Add redirect
   URIs: your viewer URL and `http://localhost:3001` (dev). Save.
4. *(No API permissions / admin consent needed — the app calls its own scope.)*

## Wire the values in

From the script output (or the portal IDs):

**Backend** (Container App env, or `.env` for local):
```
ENTRA_TENANT_ID=<tenant-id>
ENTRA_CLIENT_ID=<app-client-id>
# ENTRA_AUDIENCE defaults to api://<app-client-id>; only set to override
```

**Frontend** (build-time — see `frontend/.env.example`; baked into the static
bundle, so the SWA build step needs them):
```
NEXT_PUBLIC_API_BASE_URL=https://<your-backend>.azurecontainerapps.io
NEXT_PUBLIC_ENTRA_TENANT_ID=<tenant-id>
NEXT_PUBLIC_ENTRA_CLIENT_ID=<app-client-id>
NEXT_PUBLIC_ENTRA_API_SCOPE=api://<app-client-id>/access_as_user
```

> Leave the backend `ENTRA_*` blank to keep Entra auth **off** (local dev falls
> back to `X-API-Key` / `X-User-Id`). Setting tenant + client id turns it on;
> the API then also accepts `Authorization: Bearer` tokens.

## What comes next (this phase)

- **2b** — backend validates `Authorization: Bearer` against the tenant JWKS
  and resolves `User` by `oid` (additive; API-key path stays for the CLI).
- **2c** — viewer MSAL sign-in, attaches the token to every call, and the
  multi-source UI is retired in favor of the single configured backend.
- Deploy wiring (Bicep env + SWA build NEXT_PUBLIC_*) lands with 2c.
