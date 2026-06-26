#!/usr/bin/env bash
# One-time setup: provision the Entra app registration + federated credential +
# AcrPush role that lets the `Deploy` GitHub Actions workflow push to ACR via OIDC.
#
# Security model: GH Actions gets AcrPush scoped to the ACR resource only —
# NOT Container Apps Contributor on the subscription/resource group.
# The Container App update is handled by the ACR Task deploy-rtd (setup-acr-deploy.sh),
# which runs inside Azure with its own Managed Identity.
#
# Run this AFTER ./install.sh has provisioned the resource group + ACR.
# Re-runs are safe: the app is upserted, federated creds are dedup'd by name,
# role assignments are scope+principal idempotent.
#
# Required env:
#   GITHUB_OWNER  GitHub user/org (e.g. DonPercival0x45)
#   GITHUB_REPO   Repo name (e.g. RedTeamDashboard)
#   AZURE_RG      Resource group from install.sh
#   ACR_NAME      ACR registry name from install.sh (e.g. "rtdprodacr")
#
# After this finishes run setup-acr-deploy.sh, then trigger a deploy.
set -euo pipefail

: "${GITHUB_OWNER:?set GITHUB_OWNER}"
: "${GITHUB_REPO:?set GITHUB_REPO}"
: "${AZURE_RG:?set AZURE_RG}"
: "${ACR_NAME:?set ACR_NAME}"

APP_NAME="${APP_NAME:-rtd-github-deploy}"
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"

echo "==> ensure app registration '$APP_NAME'"
CLIENT_ID="$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv)"
if [ -z "$CLIENT_ID" ]; then
  CLIENT_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
fi
echo "    client_id=$CLIENT_ID"

echo "==> ensure service principal"
SP_OID="$(az ad sp list --filter "appId eq '$CLIENT_ID'" --query '[0].id' -o tsv)"
if [ -z "$SP_OID" ]; then
  SP_OID="$(az ad sp create --id "$CLIENT_ID" --query id -o tsv)"
fi

echo "==> ensure federated credential trusting refs/heads/main"
FC_NAME="github-main"
SUBJECT="repo:${GITHUB_OWNER}/${GITHUB_REPO}:ref:refs/heads/main"
EXISTING="$(az ad app federated-credential list --id "$CLIENT_ID" --query "[?name=='$FC_NAME'].name | [0]" -o tsv)"
if [ -z "$EXISTING" ]; then
  az ad app federated-credential create --id "$CLIENT_ID" --parameters "$(cat <<JSON
{
  "name": "$FC_NAME",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "$SUBJECT",
  "audiences": ["api://AzureADTokenExchange"]
}
JSON
)" >/dev/null
fi

# Scope: AcrPush on the specific ACR resource only.
# The previous Container Apps Contributor grant on the RG is intentionally
# removed — Container App updates now happen via the ACR Task (setup-acr-deploy.sh).
echo "==> grant 'AcrPush' on ACR '$ACR_NAME'"
ACR_ID="$(az acr show --name "$ACR_NAME" --resource-group "$AZURE_RG" --query id -o tsv)"
az role assignment create \
  --assignee-object-id "$SP_OID" \
  --assignee-principal-type ServicePrincipal \
  --role "AcrPush" \
  --scope "$ACR_ID" >/dev/null 2>&1 || true

if command -v gh >/dev/null 2>&1; then
  echo "==> write repo variables via gh"
  gh variable set AZURE_CLIENT_ID       --body "$CLIENT_ID"        --repo "$GITHUB_OWNER/$GITHUB_REPO"
  gh variable set AZURE_TENANT_ID       --body "$TENANT_ID"        --repo "$GITHUB_OWNER/$GITHUB_REPO"
  gh variable set AZURE_SUBSCRIPTION_ID --body "$SUBSCRIPTION_ID"  --repo "$GITHUB_OWNER/$GITHUB_REPO"
  gh variable set AZURE_ACR_NAME        --body "$ACR_NAME"         --repo "$GITHUB_OWNER/$GITHUB_REPO"
else
  cat <<EOF

gh CLI not found. Set these as repo VARIABLES manually
(Settings -> Secrets and variables -> Actions -> Variables):

  AZURE_CLIENT_ID       = $CLIENT_ID
  AZURE_TENANT_ID       = $TENANT_ID
  AZURE_SUBSCRIPTION_ID = $SUBSCRIPTION_ID
  AZURE_ACR_NAME        = $ACR_NAME

EOF
fi

echo ""
echo "Next: run setup-acr-deploy.sh to configure the ACR Task deploy-rtd."
echo "Then trigger a deploy:"
echo "  gh workflow run deploy.yml --repo $GITHUB_OWNER/$GITHUB_REPO"
