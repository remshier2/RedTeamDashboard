#!/usr/bin/env bash
# Red Team Dashboard — Microsoft Entra ID app registration for analyst SSO.
#
# Creates ONE app registration that is both the SPA client (analysts sign in)
# and the API it calls (it exposes a single `access_as_user` scope). The
# viewer SPA acquires an access token for that scope via MSAL and sends it as
# `Authorization: Bearer …`; the backend validates it against the tenant JWKS.
#
# A single first-party app keeps setup simple — no cross-app delegated
# permission grant and no admin-consent step. Split into two apps later if you
# ever want third parties to call the API.
#
# Usage:
#   ./setup-entra.sh --viewer-url https://<viewer>.azurestaticapps.net
#   ./setup-entra.sh --env prod --viewer-url https://… --dev-origin http://localhost:3001
#
# Prereqs:
#   - az logged in (`az login`) to the tenant that will own the app
#   - Permission to create app registrations (Application Administrator, or the
#     tenant allows users to register apps). Check: Entra admin center →
#     Identity → Users → User settings → "Users can register applications".
#   - python3 (for GUID generation + JSON)

set -euo pipefail

ENV_NAME="prod"
VIEWER_URL=""
DEV_ORIGIN="http://localhost:3001"

usage() {
    cat <<EOF
Usage: $0 --viewer-url <url> [options]

Options:
  --viewer-url URL   The deployed viewer origin (SWA). Required for prod sign-in.
  --env NAME         Env name used in the app display name (default: prod).
  --dev-origin URL   Extra SPA redirect origin for local dev (default: http://localhost:3001).
  -h, --help         Show this help.
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --viewer-url) VIEWER_URL="$2"; shift 2 ;;
        --env) ENV_NAME="$2"; shift 2 ;;
        --dev-origin) DEV_ORIGIN="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
done

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
blue()  { printf "\033[34m%s\033[0m\n" "$*"; }
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
die()   { red "error: $*" >&2; exit 1; }

command -v az >/dev/null 2>&1 || die "az CLI not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
az account show >/dev/null 2>&1 || die "not logged in — run 'az login'"

TENANT_ID="$(az account show --query tenantId -o tsv)"
DISPLAY_NAME="rtd-${ENV_NAME}-viewer"

# Redirect URIs for the SPA platform. Dedupe; drop blanks.
REDIRECTS=()
[[ -n "$VIEWER_URL" ]] && REDIRECTS+=("${VIEWER_URL%/}")
[[ -n "$DEV_ORIGIN"  ]] && REDIRECTS+=("${DEV_ORIGIN%/}")
[[ ${#REDIRECTS[@]} -eq 0 ]] && die "need at least one redirect (--viewer-url or --dev-origin)"

bold "[1/5] Creating app registration '$DISPLAY_NAME' in tenant $TENANT_ID…"
# Reuse an existing app with the same display name if present (idempotent).
APP_ID="$(az ad app list --display-name "$DISPLAY_NAME" --query '[0].appId' -o tsv)"
if [[ -z "$APP_ID" ]]; then
    APP_ID="$(az ad app create --display-name "$DISPLAY_NAME" \
        --sign-in-audience AzureADMyOrg --query appId -o tsv)"
    green "    created appId=$APP_ID"
else
    green "    reusing existing appId=$APP_ID"
fi

# Graph needs the object id (id), not the appId, for PATCH.
OBJECT_ID="$(az ad app show --id "$APP_ID" --query id -o tsv)"

bold "[2/5] Setting identifier URI api://$APP_ID…"
az ad app update --id "$APP_ID" --identifier-uris "api://$APP_ID" --only-show-errors

bold "[3/5] Exposing the 'access_as_user' scope…"
SCOPE_ID="$(python3 -c 'import uuid;print(uuid.uuid4())')"
SCOPE_BODY="$(python3 - "$SCOPE_ID" <<'PY'
import json, sys
sid = sys.argv[1]
print(json.dumps({"api": {"oauth2PermissionScopes": [{
    "id": sid,
    "value": "access_as_user",
    "type": "User",
    "isEnabled": True,
    "adminConsentDisplayName": "Access the Red Team Dashboard API",
    "adminConsentDescription": "Allows the signed-in analyst to access the RTD API on their behalf.",
    "userConsentDisplayName": "Access the Red Team Dashboard API",
    "userConsentDescription": "Allows the app to access the RTD API on your behalf.",
}]}}))
PY
)"
# Only set the scope if one doesn't already exist (avoid duplicate-value error).
EXISTING_SCOPE="$(az ad app show --id "$APP_ID" --query "api.oauth2PermissionScopes[?value=='access_as_user'] | [0].id" -o tsv)"
if [[ -z "$EXISTING_SCOPE" ]]; then
    az rest --method PATCH \
        --uri "https://graph.microsoft.com/v1.0/applications/${OBJECT_ID}" \
        --headers "Content-Type=application/json" \
        --body "$SCOPE_BODY" --only-show-errors
    green "    scope access_as_user created (id=$SCOPE_ID)"
else
    SCOPE_ID="$EXISTING_SCOPE"
    green "    scope access_as_user already present (id=$SCOPE_ID)"
fi

bold "[4/5] Configuring SPA redirect URIs…"
SPA_BODY="$(python3 - "${REDIRECTS[@]}" <<'PY'
import json, sys
print(json.dumps({"spa": {"redirectUris": sys.argv[1:]}}))
PY
)"
az rest --method PATCH \
    --uri "https://graph.microsoft.com/v1.0/applications/${OBJECT_ID}" \
    --headers "Content-Type=application/json" \
    --body "$SPA_BODY" --only-show-errors
green "    SPA redirects: ${REDIRECTS[*]}"

bold "[5/5] Ensuring a service principal exists (so analysts can sign in)…"
az ad sp show --id "$APP_ID" >/dev/null 2>&1 || az ad sp create --id "$APP_ID" --only-show-errors >/dev/null
green "    service principal ready"

SCOPE_URI="api://$APP_ID/access_as_user"

echo
green "Entra setup complete. Wire these values in:"
echo
bold "  Backend (Container App env / .env):"
echo "    ENTRA_TENANT_ID=$TENANT_ID"
echo "    ENTRA_CLIENT_ID=$APP_ID"
echo "    # ENTRA_AUDIENCE defaults to api://$APP_ID — only set to override"
echo
bold "  Frontend (build-time NEXT_PUBLIC_* — see frontend/.env.example):"
echo "    NEXT_PUBLIC_ENTRA_TENANT_ID=$TENANT_ID"
echo "    NEXT_PUBLIC_ENTRA_CLIENT_ID=$APP_ID"
echo "    NEXT_PUBLIC_ENTRA_API_SCOPE=$SCOPE_URI"
echo
echo "  Redirect URIs registered: ${REDIRECTS[*]}"
echo "  Add more later with: az ad app update --id $APP_ID (spa.redirectUris)"
