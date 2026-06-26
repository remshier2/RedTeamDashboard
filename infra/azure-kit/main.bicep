// Red Team Dashboard — Deployment Kit (subscription-scoped).
//
// Provisions, in one resource group, the per-tenant backend an operator owns:
//   - VNet with two delegated subnets (Container Apps /23, Postgres /28)
//   - Private DNS zone for Postgres VNet injection
//   - Log Analytics workspace
//   - Application Insights (workspace-based)
//   - Postgres Flexible Server — VNet-injected, no public access
//   - Key Vault (RBAC mode) with seeded secrets
//   - Azure Container Registry (Standard) for image storage
//   - Container Apps Environment (Consumption, VNet-integrated)
//   - One Container App with three colocated containers: backend, worker, redis
//   - Azure Static Web App hosting the viewer (gated by Entra ID)
//
// What's NOT here:
//   - LLM API keys: placeholders in Key Vault; operator populates after deploy.
//   - Azure OpenAI resource: provision separately and populate the KV secrets
//     if using llmProvider=azure. Default is anthropic.
//   - The admin API key: installer mints it from the running backend after
//     the deploy completes and overwrites the admin-api-key placeholder.
//
// The kit is designed for the operator to run once per engagement (or once
// total, then archive engagements via the API). Teardown is a single
// `az group delete`.

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

@description('Image tag for backend + worker (e.g. "main", "0.1.0"). The ACR Task deploy-rtd uses :main; this param is for manual/initial deploys via install.sh.')
param imageTag string = 'main'

@description('Default LLM provider for runs that don\'t pick one explicitly. The CLI/API can override per run.')
@allowed([ 'anthropic', 'openai', 'azure' ])
param llmProvider string = 'anthropic'

@description('Default Anthropic model when the run uses Anthropic without picking one. Per-run override wins.')
param anthropicModel string = 'claude-opus-4-7'

@description('Extra CORS allow-origins for the browser viewer. The kit auto-appends the in-tenant Static Web App URL; use this only to add other origins (e.g. a self-hosted viewer at a custom domain). Comma-separated.')
param extraCorsAllowOrigins string = 'http://localhost:3001,http://127.0.0.1:3001'

@description('Entra tenant + app (client) id for analyst SSO (from setup-entra.sh). Blank → Entra auth stays off; backend uses API keys. See docs/ENTRA_SETUP.md.')
param entraTenantId string = ''
param entraClientId string = ''

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

// ---------------------------------------------------------------------------
// Networking — VNet + private DNS zone for Postgres
// ---------------------------------------------------------------------------

module vnet 'modules/vnet.bicep' = {
  name: 'vnet'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

// Private DNS zone so VNet-injected Postgres is reachable by hostname from
// within the VNet. The zone name is the fixed Azure suffix for Postgres
// Flexible Server private access.
resource pgDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.postgres.database.azure.com'
  location: 'global'
  tags: tags
  scope: rg
}

resource pgDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: pgDnsZone
  name: '${namePrefix}-pg-dns-link'
  location: 'global'
  properties: {
    virtualNetwork: { id: vnet.outputs.vnetId }
    registrationEnabled: false
  }
}

// ---------------------------------------------------------------------------
// Observability
// ---------------------------------------------------------------------------

module logs 'modules/loganalytics.bicep' = {
  name: 'logs'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

module ai 'modules/appinsights.bicep' = {
  name: 'appinsights'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    workspaceId: logs.outputs.workspaceId
  }
}

// ---------------------------------------------------------------------------
// Data tier
// ---------------------------------------------------------------------------

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    adminLogin: postgresAdminLogin
    adminPassword: postgresAdminPassword
    delegatedSubnetId: vnet.outputs.postgresSubnetId
    privateDnsZoneId: pgDnsZone.id
  }
  dependsOn: [ pgDnsVnetLink ]
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

// ---------------------------------------------------------------------------
// Storage — engagement export archive (blob)
// ---------------------------------------------------------------------------

module storage 'modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Compute tier
// ---------------------------------------------------------------------------

module caenv 'modules/containerappsenv.bicep' = {
  name: 'containerappsenv'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    logAnalyticsCustomerId: logs.outputs.customerId
    logAnalyticsPrimarySharedKey: logs.outputs.primarySharedKey
    infrastructureSubnetId: vnet.outputs.containerAppsSubnetId
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

// ---------------------------------------------------------------------------
// Container Registry — images pushed by CI, pulled by Container Apps
// ---------------------------------------------------------------------------

module acr 'modules/acr.bicep' = {
  name: 'acr'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

var backendImage = '${acr.outputs.acrLoginServer}/rtd-backend:${imageTag}'
var workerImage = '${acr.outputs.acrLoginServer}/rtd-worker:${imageTag}'

// Stage 2 — secondary MCP App with scale-to-zero. Lives in the same env
// so internal DNS just works; ingress is external so the worker can
// reach it via HTTPS the same way it reaches the colocated /mcp. The
// main App below picks up its URL via the ACA_MCP_URL env var so
// Tactical can route ``lease.requires_container=True`` runs there.
module mcpApp 'modules/mcp_app.bicep' = {
  name: 'mcpApp'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    environmentId: caenv.outputs.id
    keyVaultName: kv.outputs.name
    keyVaultId: kv.outputs.id
    acrLoginServer: acr.outputs.acrLoginServer
    acrId: acr.outputs.acrId
    backendImage: backendImage
    appInsightsConnectionString: ai.outputs.connectionString
  }
}

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
    acrLoginServer: acr.outputs.acrLoginServer
    acrId: acr.outputs.acrId
    backendImage: backendImage
    workerImage: workerImage
    llmProvider: llmProvider
    anthropicModel: anthropicModel
    corsAllowOrigins: '${extraCorsAllowOrigins},${viewer.outputs.url}'
    entraTenantId: entraTenantId
    entraClientId: entraClientId
    appInsightsConnectionString: ai.outputs.connectionString
    storageAccountName: storage.outputs.storageAccountName
    acaMcpUrl: mcpApp.outputs.appUrl
    acaMcpAppEnabled: true
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

// Grant the container app's managed identity Storage Blob Data Contributor
// on the exports account so the backend can upload without a connection string.
var storageBlobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource storageAccountRef 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storage.outputs.storageAccountName
  scope: rg
}

resource appStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.outputs.storageAccountId, apps.outputs.appPrincipalId, storageBlobContributorRoleId)
  scope: storageAccountRef
  properties: {
    principalId: apps.outputs.appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobContributorRoleId
    )
  }
}

output resourceGroupName string = rg.name
output acrName string = acr.outputs.acrName
output acrLoginServer string = acr.outputs.acrLoginServer
output appFqdn string = apps.outputs.appFqdn
output appName string = apps.outputs.appName
output keyVaultName string = kv.outputs.name
output postgresFqdn string = postgres.outputs.fqdn
output viewerName string = viewer.outputs.name
output viewerUrl string = viewer.outputs.url
output appInsightsName string = ai.outputs.name
output storageAccountName string = storage.outputs.storageAccountName
output mcpAppName string = mcpApp.outputs.appName
output mcpAppFqdn string = mcpApp.outputs.appFqdn
output mcpAppUrl string = mcpApp.outputs.appUrl
