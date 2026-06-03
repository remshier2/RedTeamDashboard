// Red Team Dashboard — Deployment Kit (subscription-scoped).
//
// Provisions, in one resource group, the per-tenant backend an operator owns:
//   - Log Analytics workspace
//   - Postgres Flexible Server (Burstable B1ms)
//   - Container Apps Environment (Consumption-only, no VNet)
//   - Key Vault (RBAC mode) with seeded secrets
//   - One Container App with three colocated containers: backend, worker,
//     redis. They share `127.0.0.1` so no cross-app internal TCP is needed.
//     Single replica (minReplicas=maxReplicas=1) — sharing localhost requires
//     same pod.
//   - Azure Static Web App hosting the viewer (gated by Entra ID). The kit's
//     install.sh pushes the prebuilt static bundle after Bicep returns.
//
// What's NOT here:
//   - The viewer: hosted centrally; the operator plugs in this deployment's
//     backend URL + an API key from the central viewer's UI.
//   - Any container registry: images are public on GHCR. No auth needed.
//   - LLM API keys: placeholders in Key Vault; operator populates after deploy.
//   - The admin API key: installer mints it from the running backend after
//     the deploy completes (so the schema exists) and overwrites the
//     `admin-api-key` placeholder secret.
//
// The kit is designed for the operator to run once per engagement (or once
// total, then archive engagements via the API). Teardown is a single
// `az group delete` — see scripts/uninstall.sh.

targetScope = 'subscription'

@description('Short env name; becomes part of every resource name (e.g. "prod", "ops").')
param env string = 'prod'

@description('Azure region for everything.')
param location string = 'eastus2'

@description('Resource group name. Defaults to rtd-<env>.')
param resourceGroupName string = 'rtd-${env}'

@description('Postgres admin username.')
param postgresAdminLogin string = 'rtdadmin'

@description('Postgres admin password. Pass via @secure() bicepparam or CLI prompt.')
@secure()
param postgresAdminPassword string

@description('GHCR repository owner (e.g. "donpercival0x45"). The kit pulls images from ghcr.io/<owner>/rtd-{backend,worker}:<tag>.')
param imageRepoOwner string = 'donpercival0x45'

@description('Image tag for backend + worker (e.g. "0.1.0", "v0.1.0", "main"). Bump on each release.')
param imageTag string = 'latest'

@description('Default LLM provider for runs that don\'t pick one explicitly. The CLI/API can override per run.')
@allowed([ 'anthropic', 'openai', 'azure' ])
param llmProvider string = 'anthropic'

@description('Default Anthropic model when the run uses Anthropic without picking one. Per-run override wins.')
param anthropicModel string = 'claude-opus-4-7'

@description('Extra CORS allow-origins for the browser viewer. The kit auto-appends the in-tenant Static Web App URL; use this only to add other origins (e.g. a self-hosted viewer at a custom domain). Comma-separated.')
param extraCorsAllowOrigins string = 'http://localhost:3001,http://127.0.0.1:3001'

var namePrefix = 'rtd-${env}'
var tags = {
  app: 'red-team-dashboard'
  env: env
  managedBy: 'bicep-kit'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module logs 'modules/loganalytics.bicep' = {
  name: 'logs'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    adminLogin: postgresAdminLogin
    adminPassword: postgresAdminPassword
  }
}

module caenv 'modules/containerappsenv.bicep' = {
  name: 'containerappsenv'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    logAnalyticsCustomerId: logs.outputs.customerId
    logAnalyticsPrimarySharedKey: logs.outputs.primarySharedKey
  }
}

module viewer 'modules/viewer.bicep' = {
  name: 'viewer'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

module kv 'modules/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    postgresPassword: postgresAdminPassword
    databaseUrl: postgres.outputs.sqlAlchemyUrl
  }
}

// Image refs the container apps pull from GHCR. Public; no registry creds.
var backendImage = 'ghcr.io/${imageRepoOwner}/rtd-backend:${imageTag}'
var workerImage = 'ghcr.io/${imageRepoOwner}/rtd-worker:${imageTag}'

module apps 'modules/containerapps.bicep' = {
  name: 'containerapps'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    environmentId: caenv.outputs.id
    keyVaultName: kv.outputs.name
    keyVaultId: kv.outputs.id
    backendImage: backendImage
    workerImage: workerImage
    llmProvider: llmProvider
    anthropicModel: anthropicModel
    // Auto-append the in-tenant viewer's URL so the browser at that origin
    // can call the backend without any manual CORS plumbing.
    corsAllowOrigins: '${extraCorsAllowOrigins},${viewer.outputs.url}'
  }
}

output resourceGroupName string = rg.name
output appFqdn string = apps.outputs.appFqdn
output appName string = apps.outputs.appName
output keyVaultName string = kv.outputs.name
output postgresFqdn string = postgres.outputs.fqdn
output viewerName string = viewer.outputs.name
output viewerUrl string = viewer.outputs.url
