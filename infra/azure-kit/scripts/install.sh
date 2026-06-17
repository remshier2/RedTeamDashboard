#!/usr/bin/env bash
# Red Team Dashboard — Deployment Kit installer.
#
# One-shot install: provisions every Azure resource the kit needs in the
# subscription you've already selected with `az account set`. Re-runnable —
# Bicep deploys are idempotent on resource names. Re-running with a new
# --image-tag rolls the apps without recreating the data plane.
#
# Usage:
#     ./install.sh                              # interactive
#     ./install.sh --env prod --location centralus --image-tag v0.1.0
#
# Prereqs (also enforced below):
#   - az logged in: `az login`
#   - az subscription selected: `az account set --subscription <name>`
#   - Bicep CLI installed: `az bicep install`
#   - openssl on PATH (for generating the postgres password)
#   - docker on PATH (for building + deploying the viewer bundle)
#
# LLM API keys:
#   Pass --anthropic-key or set ANTHROPIC_API_KEY in the environment.
#   Pass --openai-key or set OPENAI_API_KEY in the environment.
#   Keys are written directly to Key Vault — never stored in the script.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults + arg parsing
# ---------------------------------------------------------------------------

ENV_NAME="prod"
LOCATION="eastus2"
IMAGE_REPO_OWNER="donpercival0x45"
IMAGE_TAG="latest"
LLM_PROVIDER="anthropic"
PG_PW=""
ENTRA_TENANT_ID=""
ENTRA_CLIENT_ID=""
ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
OPENAI_KEY="${OPENAI_API_KEY:-}"
NON_INTERACTIVE=false

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --env NAME              Short env name; used in every resource name (default: prod)
  --location REGION       Azure region (default: eastus2)
  --image-repo-owner OWNER GHCR owner where rtd-{backend,worker} are published (default: donpercival0x45)
  --image-tag TAG         Image tag to deploy (default: latest)
  --llm-provider P        anthropic | openai | azure (default: anthropic)
  --postgres-password PW  Provide the postgres password; otherwise one is generated.
  --anthropic-key KEY     Anthropic API key to store in Key Vault. Falls back to
                          ANTHROPIC_API_KEY env var. Prompted if neither is set and
                          llm-provider is anthropic.
  --openai-key KEY        OpenAI API key to store in Key Vault. Falls back to
                          OPENAI_API_KEY env var.
  --entra-tenant-id ID    Entra tenant id for analyst SSO (from setup-entra.sh). Optional.
  --entra-client-id ID    Entra app (client) id for analyst SSO. Optional.
  --yes                   Skip the confirmation prompt; useful in CI/automation.
  -h, --help              Show this help.
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env)               ENV_NAME="$2";         shift 2 ;;
        --location)          LOCATION="$2";          shift 2 ;;
        --image-repo-owner)  IMAGE_REPO_OWNER="$2";  shift 2 ;;
        --image-tag)         IMAGE_TAG="$2";          shift 2 ;;
        --llm-provider)      LLM_PROVIDER="$2";       shift 2 ;;
        --postgres-password) PG_PW="$2";              shift 2 ;;
        --anthropic-key)     ANTHROPIC_KEY="$2";      shift 2 ;;
        --openai-key)        OPENAI_KEY="$2";          shift 2 ;;
        --entra-tenant-id)   ENTRA_TENANT_ID="$2";    shift 2 ;;
        --entra-client-id)   ENTRA_CLIENT_ID="$2";    shift 2 ;;
        --yes)               NON_INTERACTIVE=true;     shift ;;
        -h|--help)           usage ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
done

RG_NAME="rtd-${ENV_NAME}"
DEPLOY_NAME="rtd-${ENV_NAME}-$(date +%Y%m%d%H%M%S)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(dirname "$HERE")"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
blue()  { printf "\033[34m%s\033[0m\n" "$*"; }
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }

die() { red "error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

# Run a command inside the backend container. `az containerapp exec` requires
# a TTY; `script` provides one. Syntax differs between Linux (util-linux) and
# macOS (BSD script).
container_exec() {
    local cmd="$1"
    if [[ "$(uname)" == "Darwin" ]]; then
        script -q /dev/null az containerapp exec \
            -n "$APP_NAME" -g "$RG_OUT" --container backend --command "$cmd"
    else
        script -qc "az containerapp exec \
            -n '$APP_NAME' -g '$RG_OUT' --container backend --command '$cmd'" /dev/null
    fi
}

# ---------------------------------------------------------------------------
# Prereq checks
# ---------------------------------------------------------------------------

bold "[1/7] Checking prerequisites…"
need az
need openssl
az bicep version >/dev/null 2>&1 || die "Bicep CLI missing — run 'az bicep install'"

SUB_INFO="$(az account show -o json 2>/dev/null || true)"
[[ -z "$SUB_INFO" ]] && die "not logged in. Run 'az login' first."
SUB_NAME="$(echo "$SUB_INFO" | python3 -c 'import sys,json;print(json.load(sys.stdin)["name"])')"
TENANT_ID="$(echo "$SUB_INFO" | python3 -c 'import sys,json;print(json.load(sys.stdin)["tenantId"])')"

echo "    Subscription: $SUB_NAME"
echo "    Tenant:       $TENANT_ID"
echo "    Region:       $LOCATION"
echo "    Resource group: $RG_NAME"
echo "    Image:        ghcr.io/$IMAGE_REPO_OWNER/rtd-{backend,worker}:$IMAGE_TAG"
echo "    LLM provider: $LLM_PROVIDER"
echo

# Prompt for LLM key if needed and not already provided
if [[ "$LLM_PROVIDER" == "anthropic" && -z "$ANTHROPIC_KEY" && "$NON_INTERACTIVE" != "true" ]]; then
    read -rsp "    Anthropic API key (sk-ant-…): " ANTHROPIC_KEY
    echo
    [[ -z "$ANTHROPIC_KEY" ]] && die "Anthropic API key is required when llm-provider=anthropic"
fi

if [[ "$NON_INTERACTIVE" != "true" ]]; then
    read -rp "Proceed with this configuration? [y/N] " ack
    [[ "$ack" =~ ^[Yy]$ ]] || { echo "aborted."; exit 1; }
fi

# ---------------------------------------------------------------------------
# Postgres password
# ---------------------------------------------------------------------------

if [[ -z "$PG_PW" ]]; then
    PG_PW="$(openssl rand -base64 24 | tr -d '=+/' | cut -c1-24)Aa1!"
    bold "[2/7] Generated postgres password (stored in Key Vault as 'postgres-password')."
else
    bold "[2/7] Using provided postgres password."
fi

# ---------------------------------------------------------------------------
# Bicep deploy
# ---------------------------------------------------------------------------

bold "[3/7] Running Bicep deploy '$DEPLOY_NAME'… (5-10 minutes for first run)"

GHCR_IMAGE_TAG="${IMAGE_TAG#v}"

az deployment sub create \
    --name "$DEPLOY_NAME" \
    --location "$LOCATION" \
    --template-file "$KIT_ROOT/main.bicep" \
    --parameters env="$ENV_NAME" \
    --parameters location="$LOCATION" \
    --parameters postgresAdminPassword="$PG_PW" \
    --parameters imageRepoOwner="$IMAGE_REPO_OWNER" \
    --parameters imageTag="$GHCR_IMAGE_TAG" \
    --parameters llmProvider="$LLM_PROVIDER" \
    --parameters entraTenantId="$ENTRA_TENANT_ID" \
    --parameters entraClientId="$ENTRA_CLIENT_ID" \
    --only-show-errors \
    -o none

# ---------------------------------------------------------------------------
# Pull outputs
# ---------------------------------------------------------------------------

bold "[4/7] Capturing deployment outputs…"

OUTPUTS="$(az deployment sub show -n "$DEPLOY_NAME" --query properties.outputs -o json)"

RG_OUT="$(echo "$OUTPUTS"     | python3 -c 'import sys,json;print(json.load(sys.stdin)["resourceGroupName"]["value"])')"
APP_FQDN="$(echo "$OUTPUTS"   | python3 -c 'import sys,json;print(json.load(sys.stdin)["appFqdn"]["value"])')"
APP_NAME="$(echo "$OUTPUTS"   | python3 -c 'import sys,json;print(json.load(sys.stdin)["appName"]["value"])')"
KV_NAME="$(echo "$OUTPUTS"    | python3 -c 'import sys,json;print(json.load(sys.stdin)["keyVaultName"]["value"])')"
VIEWER_NAME="$(echo "$OUTPUTS" | python3 -c 'import sys,json;print(json.load(sys.stdin)["viewerName"]["value"])')"
VIEWER_URL="$(echo "$OUTPUTS"  | python3 -c 'import sys,json;print(json.load(sys.stdin)["viewerUrl"]["value"])')"

echo "    resource group:  $RG_OUT"
echo "    app FQDN:        https://$APP_FQDN"
echo "    key vault:       $KV_NAME"
echo "    viewer URL:      $VIEWER_URL"

# ---------------------------------------------------------------------------
# Wait for backend health
# The backend startup command runs `alembic upgrade head` before uvicorn
# starts, so by the time /health returns green the schema is initialized.
# The first revision also races KV identity propagation — bump it so the
# second revision picks up the now-propagated managed identity role.
# ---------------------------------------------------------------------------

bold "[5/7] Forcing fresh revision + waiting for the app to come healthy…"
echo "    (migrations run automatically on startup; waiting for schema + DB to be ready)"

REV_BUMP="$(date +%s)"
az containerapp update -n "$APP_NAME" -g "$RG_OUT" \
    --container-name backend \
    --set-env-vars "RTD_REVISION_BUMP=$REV_BUMP" --only-show-errors -o none

for i in {1..40}; do
    if curl -sf "https://$APP_FQDN/health" >/dev/null 2>&1; then
        green "    app is up — schema initialized."
        break
    fi
    [[ $i -eq 40 ]] && die "app never became healthy. Check: az containerapp logs show -n $APP_NAME -g $RG_OUT --container backend"
    sleep 6
done

# ---------------------------------------------------------------------------
# Deploy the viewer static bundle to the Static Web App
# ---------------------------------------------------------------------------

bold "[5.5/7] Building + deploying viewer to Static Web App…"
SWA_SKIPPED=false
FRONTEND_DIR="$(cd "$KIT_ROOT/../.." && pwd)/frontend"
if ! command -v docker >/dev/null 2>&1; then
    red "    skipped — docker not on PATH; install Docker then re-run"
    red "    or deploy manually: SWA name=$VIEWER_NAME, see docs/DEPLOY.md"
    SWA_SKIPPED=true
elif [[ ! -d "$FRONTEND_DIR" ]]; then
    red "    skipped — viewer source not found at $FRONTEND_DIR"
    red "    (run install.sh from inside a repo checkout). See docs/DEPLOY.md"
    SWA_SKIPPED=true
else
    ENTRA_SCOPE=""
    [[ -n "$ENTRA_CLIENT_ID" ]] && ENTRA_SCOPE="api://$ENTRA_CLIENT_ID/access_as_user"
    [[ -n "$ENTRA_CLIENT_ID" ]] && SSO_STATE="on" || SSO_STATE="off (API-key auth)"
    TMP_DIR="$(mktemp -d)"
    tar -C "$FRONTEND_DIR" --exclude=node_modules --exclude=.next --exclude=out -cf - . \
        | tar -C "$TMP_DIR" -xf -
    echo "    building viewer (API=https://$APP_FQDN, SSO=$SSO_STATE)…"
    if docker run --rm \
        -e NEXT_OUTPUT=export \
        -e NEXT_PUBLIC_API_BASE_URL="https://$APP_FQDN" \
        -e NEXT_PUBLIC_ENTRA_TENANT_ID="$ENTRA_TENANT_ID" \
        -e NEXT_PUBLIC_ENTRA_CLIENT_ID="$ENTRA_CLIENT_ID" \
        -e NEXT_PUBLIC_ENTRA_API_SCOPE="$ENTRA_SCOPE" \
        -v "$TMP_DIR:/app" -w /app node:lts \
        sh -c "npm ci --no-audit --no-fund && npm run build"; then
        DEPLOY_TOKEN="$(az staticwebapp secrets list -n "$VIEWER_NAME" -g "$RG_OUT" --query 'properties.apiKey' -o tsv)"
        docker run --rm -v "$TMP_DIR/out:/work" node:lts sh -c \
            "cd /tmp && SWA_CLI_TELEMETRY_OPTOUT=1 npx -y @azure/static-web-apps-cli@latest \
                deploy /work --deployment-token $DEPLOY_TOKEN \
                --env production --no-use-keychain"
        green "    viewer deployed."
    else
        red "    viewer build failed — see output above. SWA left empty."
        SWA_SKIPPED=true
    fi
    docker run --rm -v "$TMP_DIR:/t" node:lts chown -R "$(id -u):$(id -g)" /t || true
    rm -rf "$TMP_DIR"
fi

# ---------------------------------------------------------------------------
# Bootstrap — mint admin key + store secrets in Key Vault
# ---------------------------------------------------------------------------

bold "[6/7] Bootstrapping — minting admin API key and storing secrets…"

# Grant the logged-in user Key Vault Secrets Officer so we can write secrets.
# Subscription Owner doesn't auto-inherit KV data-plane in RBAC mode.
ME="$(az ad signed-in-user show --query id -o tsv)"
KV_ID="$(az keyvault show -n "$KV_NAME" -g "$RG_OUT" --query id -o tsv)"
az role assignment create \
    --role "Key Vault Secrets Officer" \
    --assignee "$ME" \
    --scope "$KV_ID" \
    --only-show-errors -o none 2>/dev/null || true   # idempotent — ignore if already assigned
echo "    waiting for KV role to propagate…"
sleep 30

# Mint the bootstrap admin key. The token prints to stdout — copy it.
echo
blue "    Minting bootstrap admin API key — COPY the rtd_… token that appears below:"
echo
container_exec 'python -m app.scripts.mint_api_key --name bootstrap --scope admin'
echo

if [[ "$NON_INTERACTIVE" != "true" ]]; then
    read -rsp "    Paste the rtd_… token to store it in Key Vault (hidden): " ADMIN_KEY
    echo
    if [[ -n "$ADMIN_KEY" ]]; then
        az keyvault secret set --vault-name "$KV_NAME" --name admin-api-key \
            --value "$ADMIN_KEY" --only-show-errors -o none
        green "    admin-api-key stored in Key Vault."
    else
        red "    no token provided — store it manually: az keyvault secret set --vault-name $KV_NAME --name admin-api-key --value '<token>'"
    fi
fi

# Store LLM keys
if [[ -n "$ANTHROPIC_KEY" ]]; then
    az keyvault secret set --vault-name "$KV_NAME" --name anthropic-api-key \
        --value "$ANTHROPIC_KEY" --only-show-errors -o none
    green "    anthropic-api-key stored in Key Vault."
fi
if [[ -n "$OPENAI_KEY" ]]; then
    az keyvault secret set --vault-name "$KV_NAME" --name openai-api-key \
        --value "$OPENAI_KEY" --only-show-errors -o none
    green "    openai-api-key stored in Key Vault."
fi

# Restart so the app picks up the newly stored secrets
bold "[7/7] Restarting app to pick up Key Vault secrets…"
REV="$(az containerapp revision list -n "$APP_NAME" -g "$RG_OUT" \
    --query '[?properties.active].name | [0]' -o tsv)"
az containerapp revision restart -n "$APP_NAME" -g "$RG_OUT" \
    --revision "$REV" --only-show-errors -o none
green "    restart triggered."

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
green "Deploy complete."
echo
echo "  API URL:        https://$APP_FQDN"
echo "  Viewer URL:     $VIEWER_URL"
echo "  Resource group: $RG_OUT"
echo "  Key Vault:      $KV_NAME"
echo "  Tenant:         $TENANT_ID"
echo

if [[ "$SWA_SKIPPED" != "true" ]]; then
    ENC_URL="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1], safe=''))" "https://$APP_FQDN")"
    ENC_NAME="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1], safe=''))" "$ENV_NAME")"
    echo "Quick-start link for your teammate (pre-fills the source form):"
    blue "  $VIEWER_URL/sources?url=$ENC_URL&name=$ENC_NAME"
    echo
fi

echo "Mint a scoped key for each analyst:"
echo "  az containerapp exec -n $APP_NAME -g $RG_OUT --container backend \\"
echo "      --command 'python -m app.scripts.mint_api_key --name <name> --scope cli'"
echo

if [[ -z "$ANTHROPIC_KEY" && -z "$OPENAI_KEY" ]]; then
    red "  No LLM key was stored. Runs will fail until you add one:"
    echo "  az keyvault secret set --vault-name $KV_NAME --name anthropic-api-key --value 'sk-ant-…'"
    echo "  Then restart: az containerapp revision restart -n $APP_NAME -g $RG_OUT --revision \$(az containerapp revision list -n $APP_NAME -g $RG_OUT --query '[0].name' -o tsv)"
fi

echo
bold "Connect Claude Code (MCP) — paste your rtd_… token from step 6:"
blue "  claude mcp add rtd-${ENV_NAME} \\"
blue "      --transport sse \\"
blue "      --url https://$APP_FQDN/mcp/sse \\"
blue "      --header 'X-API-Key: <your-rtd-token>'"
echo "  Then: claude  (start a session and ask 'What engagements do I have?')"
