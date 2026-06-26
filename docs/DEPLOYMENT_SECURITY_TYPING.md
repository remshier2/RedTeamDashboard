# Deployment Security & Incremental Typing Improvements

## Overview
Focused improvements addressing concrete pain points rather than hypothetical future flexibility.

**Total scope: 3-4 weeks**

---

## 1. Pull-based ACR Deployment ✓ implemented

### Problem Statement
- GitHub Actions had `Container Apps Contributor` on the resource group
- CI/CD compromise = subscription-level write access to all Container Apps
- Azure credentials in CI/CD represent genuine security risk

### Solution
- Azure Container Registry (Standard SKU) added to the Bicep kit (`modules/acr.bicep`)
- ACR Task `deploy-rtd` triggers on `rtd-backend:main` push and runs `az containerapp update` via system-assigned Managed Identity
- GH Actions OIDC principal scoped to `AcrPush` on the ACR only — no Container Apps access

### Files changed
- `infra/azure-kit/modules/acr.bicep` — new ACR module
- `infra/azure-kit/modules/containerapps.bicep` — ACR registry auth + AcrPull grant
- `infra/azure-kit/modules/mcp_app.bicep` — same
- `infra/azure-kit/main.bicep` — ACR module wired in, image refs updated
- `infra/azure-kit/acr-deploy-task.yaml` — ACR Task definition
- `infra/azure-kit/scripts/setup-acr-deploy.sh` — one-time ACR Task setup
- `infra/azure-kit/scripts/setup-github-deploy.sh` — now grants AcrPush, not Container Apps Contributor
- `.github/workflows/deploy.yml` — push to ACR, Container App update removed

### Security model after
- GH Actions compromise → attacker can push malicious images to ACR (same blast radius as GHCR push, no worse)
- GH Actions cannot modify Container App configuration, RBAC, secrets, or env vars
- Container App updates are mediated by the ACR Task inside Azure, using its own Managed Identity
- Audit trail: ACR push logs + ACR Task run logs (separate from GH Actions logs)

---

## 2. ImporterProtocol (when source #4 lands)

### When Needed
- When fourth importer source is required

### Solution
- Single typing.Protocol file (~2 days)
- Type safety for importer contract
- No plugin framework needed

---

## 3. ExecutorProtocol ✓ implemented

### Solution
- `ExecutorProtocol` replaces the `MCPExecutor` type alias in `backend/app/worker/mcp_executor.py`
- `runtime_checkable` Protocol so dispatch-node injection sites can be type-checked
- `MCPExecutor` kept as an alias for backward compat with existing call sites

---

## 4. Feature Flag Helper (when Stage 3 ramps)

### When Needed
- When Stage 3 needs to ramp container routing

### Solution
- Simple env var + helper function (~1 day)
- Safe rollout of risky routing changes

---

## Philosophy

Follow project principle: 'Don't add features, refactor, or introduce abstractions beyond what the task requires.'

Each piece independently justifiable. No premature abstractions.

---

*Document Version: 1.1*
*Items 1 and 3 implemented; Items 2 and 4 remain gated on future work*
