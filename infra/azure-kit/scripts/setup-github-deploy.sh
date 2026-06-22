#!/usr/bin/env bash
# One-time setup: provision the Entra app registration + federated credential +
# role assignment that lets the `Deploy` GitHub Actions workflow log into Azure
# via OIDC and roll the Container App.
#
# Run this AFTER ./install.sh has provisioned the resource group + Container App.
# Re-runs are safe: the app is upserted, federated creds are dedup'd by name,
# role assignments are scope+principal idempotent.
#
# Required env:
#   GITHUB_OWNER  GitHub user/org (e.g. DonPercival0x45)
#   GITHUB_REPO   Repo name (e.g. RedTeamDashboard)
#   AZURE_RG      Resource group from install.sh
#   AZURE_APP     Container App name from install.sh
#
# After this finishes you can trigger a deploy from the GitHub Actions tab.
set -euo pipefail

: "${GITHUB_OWNER:?set GITHUB_OWNER}"
: "${GITHUB_REPO:?set GITHUB_REPO}"
: "${AZURE_RG:?set AZURE_RG}"
: "${AZURE_APP:?set AZURE_APP}"

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

echo "==> grant 'Container Apps Contributor' on '$AZURE_RG'"
RG_SCOPE="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$AZURE_RG"
az role assignment create \
  --assignee-object-id "$SP_OID" \
  --assignee-principal-type ServicePrincipal \
  --role "Container Apps Contributor" \
  --scope "$RG_SCOPE" >/dev/null 2>&1 || true

if command -v gh >/dev/null 2>&1; then
  echo "==> write repo variables via gh"
  gh variable set AZURE_CLIENT_ID       --body "$CLIENT_ID"        --repo "$GITHUB_OWNER/$GITHUB_REPO"
  gh variable set AZURE_TENANT_ID       --body "$TENANT_ID"        --repo "$GITHUB_OWNER/$GITHUB_REPO"
  gh variable set AZURE_SUBSCRIPTION_ID --body "$SUBSCRIPTION_ID"  --repo "$GITHUB_OWNER/$GITHUB_REPO"
  gh variable set AZURE_RG              --body "$AZURE_RG"         --repo "$GITHUB_OWNER/$GITHUB_REPO"
  gh variable set AZURE_APP_NAME        --body "$AZURE_APP"        --repo "$GITHUB_OWNER/$GITHUB_REPO"
else
  cat <<EOF

gh CLI not found. Set these as repo VARIABLES manually
(Settings -> Secrets and variables -> Actions -> Variables):

  AZURE_CLIENT_ID       = $CLIENT_ID
  AZURE_TENANT_ID       = $TENANT_ID
  AZURE_SUBSCRIPTION_ID = $SUBSCRIPTION_ID
  AZURE_RG              = $AZURE_RG
  AZURE_APP_NAME        = $AZURE_APP

EOF
fi

echo ""
echo "Done. Trigger a deploy:"
echo "  gh workflow run deploy.yml --repo $GITHUB_OWNER/$GITHUB_REPO"
echo "  (or click 'Run workflow' under Actions -> Deploy)"
