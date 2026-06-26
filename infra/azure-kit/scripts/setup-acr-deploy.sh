#!/usr/bin/env bash
# One-time setup: create the ACR Task deploy-rtd that fires on rtd-backend:main
# pushes and rolls the Container App using a system-assigned Managed Identity.
#
# This completes the pull-based deployment model:
#   GH Actions (AcrPush) → push :main to ACR
#                        → ACR Task triggers (this task)
#                        → Task MI (Container Apps Contributor) → containerapp update
#
# GH Actions only needs AcrPush — it never touches the Container App directly.
#
# Run this AFTER ./install.sh + setup-github-deploy.sh.
# Re-runs are safe: task creation falls back to update if the task exists.
#
# Required env:
#   AZURE_RG   Resource group from install.sh
#   AZURE_APP  Container App name from install.sh
#   ACR_NAME   ACR registry name from install.sh (e.g. "rtdprodacr")
set -euo pipefail

: "${AZURE_RG:?set AZURE_RG}"
: "${AZURE_APP:?set AZURE_APP}"
: "${ACR_NAME:?set ACR_NAME}"

SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TASK_FILE="$(dirname "$0")/../acr-deploy-task.yaml"

echo "==> create/update ACR Task 'deploy-rtd' on registry '$ACR_NAME'"
if az acr task show --registry "$ACR_NAME" --name deploy-rtd &>/dev/null; then
  az acr task update \
    --registry "$ACR_NAME" \
    --name deploy-rtd \
    --set APP_NAME="$AZURE_APP" \
    --set APP_RG="$AZURE_RG" \
    --set SUBSCRIPTION="$SUBSCRIPTION_ID"
  echo "    updated existing task"
else
  az acr task create \
    --registry "$ACR_NAME" \
    --name deploy-rtd \
    --image-trigger rtd-backend \
    --trigger-enabled true \
    --file "$TASK_FILE" \
    --context /dev/null \
    --assign-identity "[system]" \
    --set APP_NAME="$AZURE_APP" \
    --set APP_RG="$AZURE_RG" \
    --set SUBSCRIPTION="$SUBSCRIPTION_ID"
  echo "    created task"
fi

echo "==> retrieve task system identity"
TASK_PRINCIPAL_ID="$(az acr task show \
  --registry "$ACR_NAME" \
  --name deploy-rtd \
  --query identity.principalId -o tsv)"
echo "    principal_id=$TASK_PRINCIPAL_ID"

echo "==> grant 'Container Apps Contributor' on RG '$AZURE_RG' to task identity"
RG_SCOPE="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$AZURE_RG"
az role assignment create \
  --assignee-object-id "$TASK_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Container Apps Contributor" \
  --scope "$RG_SCOPE" >/dev/null 2>&1 || true
echo "    granted (idempotent)"

echo ""
echo "Done. The ACR Task 'deploy-rtd' fires automatically when rtd-backend:main is pushed."
echo "Monitor runs: az acr task list-runs --registry $ACR_NAME -o table"
echo "Trigger manually: az acr task run --registry $ACR_NAME --name deploy-rtd"
