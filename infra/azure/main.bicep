// Red Team Dashboard — Phase 0 Azure deploy (subscription-scoped).
//
// Creates a per-environment resource group and provisions:
//   - VNet with two delegated subnets (Container Apps /23, Postgres /28)
//   - Private DNS zone for Postgres VNet injection
//   - Log Analytics workspace
//   - Application Insights (workspace-based)
//   - Azure Container Registry (Basic)
//   - Azure Database for PostgreSQL Flexible Server — VNet-injected, no public access
//   - Azure Cache for Redis (Basic C0)
//   - Key Vault (RBAC mode) with seeded secrets
//   - Container Apps Environment + 3 apps (backend, worker, frontend)
//
// First deploy: containers will fail until ACR images exist. See
// `infra/azure/README.md` for the build+push+revision-roll sequence.
//
// Azure OpenAI: NOT provisioned here. If llmProvider=azure, create the AOAI
// resource separately and populate the KV secrets (azure-openai-api-key,
// azure-openai-endpoint, azure-openai-deployment) after the deploy.

targetScope = 'subscription'

@description('Short env name; becomes part of every resource name (e.g. "dev", "prod").')
param env string = 'dev'

@description('Azure region for everything. Stick to one region for Phase 0.')
param location string = 'eastus'

@description('Resource group name. Defaults to rtd-<env>.')
param resourceGroupName string = 'rtd-${env}'

@description('Postgres admin username.')
param postgresAdminLogin string = 'rtdadmin'

@description('Postgres admin password. Pass via @secure() bicepparam or CLI prompt.')
@secure()
param postgresAdminPassword string

@description('Tag for backend image in ACR (e.g. "0.0.1", "main-abc1234").')
param backendImageTag string = 'placeholder'

@description('Tag for worker image in ACR.')
param workerImageTag string = 'placeholder'

@description('Tag for frontend image in ACR.')
param frontendImageTag string = 'placeholder'

@description('LLM_PROVIDER env value injected into backend + worker. Default is anthropic; set to azure only after provisioning an Azure OpenAI resource and populating KV secrets.')
@allowed([ 'azure', 'anthropic', 'ollama' ])
param llmProvider string = 'anthropic'

@description('ANTHROPIC_MODEL env value (used when LLM_PROVIDER=anthropic).')
param anthropicModel string = 'claude-opus-4-7'

var namePrefix = 'rtd-${env}'
var tags = {
  app: 'red-team-dashboard'
  env: env
  managedBy: 'bicep'
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

module acr 'modules/acr.bicep' = {
  name: 'acr'
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
    delegatedSubnetId: vnet.outputs.postgresSubnetId
    privateDnsZoneId: pgDnsZone.id
  }
  dependsOn: [ pgDnsVnetLink ]
}

module redis 'modules/redis.bicep' = {
  name: 'redis'
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
    redisUrl: redis.outputs.url
  }
}

// ---------------------------------------------------------------------------
// Compute tier
// ---------------------------------------------------------------------------

var backendImage = '${acr.outputs.loginServer}/rtd-backend:${backendImageTag}'
var workerImage = '${acr.outputs.loginServer}/rtd-worker:${workerImageTag}'
var frontendImage = '${acr.outputs.loginServer}/rtd-frontend:${frontendImageTag}'

module apps 'modules/containerapps.bicep' = {
  name: 'containerapps'
  scope: rg
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    logAnalyticsCustomerId: logs.outputs.customerId
    logAnalyticsPrimarySharedKey: logs.outputs.primarySharedKey
    infrastructureSubnetId: vnet.outputs.containerAppsSubnetId
    acrLoginServer: acr.outputs.loginServer
    acrId: acr.outputs.id
    keyVaultName: kv.outputs.name
    keyVaultId: kv.outputs.id
    backendImage: backendImage
    workerImage: workerImage
    frontendImage: frontendImage
    llmProvider: llmProvider
    anthropicModel: anthropicModel
    appInsightsConnectionString: ai.outputs.connectionString
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output resourceGroupName string = rg.name
output acrLoginServer string = acr.outputs.loginServer
output backendFqdn string = apps.outputs.backendFqdn
output frontendFqdn string = apps.outputs.frontendFqdn
output keyVaultName string = kv.outputs.name
output postgresFqdn string = postgres.outputs.fqdn
output redisHostName string = redis.outputs.hostName
