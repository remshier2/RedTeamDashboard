// Key Vault — single source of truth for runtime secrets.
//
// RBAC mode (not access policies) so the container app's system-assigned
// identity can be granted `Key Vault Secrets User` post-hoc without
// rewriting any access-policy block. Secrets seeded here:
//
//   - postgres-password            (admin password, written by main.bicep)
//   - database-url                 (full SQLAlchemy URL with sslmode=require)
//   - anthropic-api-key            (placeholder; rotate post-deploy)
//   - openai-api-key               (placeholder; rotate post-deploy)
//   - azure-openai-api-key         (placeholder; AOAI is optional)
//   - azure-openai-endpoint        (placeholder)
//   - azure-openai-deployment      (placeholder)
//   - admin-api-key                (placeholder; installer overwrites with the
//                                   bootstrap key minted from the running
//                                   backend AFTER the deploy completes)
//
// The PLACEHOLDER markers are intentional — the deploy is idempotent on the
// secrets it knows at template time, and the installer/operator fills in the
// real values via `az keyvault secret set` once.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object
param tenantId string = subscription().tenantId

@secure()
param postgresPassword string
@secure()
param databaseUrl string

// KV names must be 3-24 chars; trim if needed.
var vaultName = take('${namePrefix}-kv', 24)

resource kv 'Microsoft.KeyVault/vaults@2024-04-01-preview' = {
  name: vaultName
  location: location
  tags: tags
  properties: {
    tenantId: tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    publicNetworkAccess: 'Enabled'
  }
}

resource sPostgresPassword 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'postgres-password'
  properties: { value: postgresPassword }
}

resource sDatabaseUrl 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'database-url'
  properties: { value: databaseUrl }
}

resource sAnthropicKey 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'anthropic-api-key'
  properties: { value: 'PLACEHOLDER-set-after-deploy' }
}

resource sOpenAiKey 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'openai-api-key'
  properties: { value: 'PLACEHOLDER-set-after-deploy' }
}

resource sAzureOpenAiKey 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'azure-openai-api-key'
  properties: { value: 'PLACEHOLDER-set-after-deploy' }
}

resource sAzureOpenAiEndpoint 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'azure-openai-endpoint'
  properties: { value: 'PLACEHOLDER-set-after-deploy' }
}

resource sAzureOpenAiDeployment 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'azure-openai-deployment'
  properties: { value: 'PLACEHOLDER-set-after-deploy' }
}

// Placeholder; the installer overwrites this with the bootstrap admin key
// minted from the running backend after the deploy.
resource sAdminApiKey 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'admin-api-key'
  properties: { value: 'PLACEHOLDER-installer-will-overwrite' }
}

// Stage 3+1: the worker hard-requires this. Installer mints a cli-scoped
// key after deploy (same pattern as admin-api-key) and overwrites this
// placeholder. The worker fails fast at boot until it's a real key.
resource sWorkerMcpApiKey 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'worker-mcp-api-key'
  properties: { value: 'PLACEHOLDER-installer-will-overwrite' }
}

output id string = kv.id
output name string = kv.name
output uri string = kv.properties.vaultUri
