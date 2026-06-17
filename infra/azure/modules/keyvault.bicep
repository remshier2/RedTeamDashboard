// Key Vault — single source of truth for runtime secrets.
//
// RBAC mode (not access policies) so container app system-assigned identities
// can be granted `Key Vault Secrets User` post-hoc without rewriting the
// access policy block. Phase 0 secrets:
//
//   - postgres-password         (admin password, written by main.bicep)
//   - database-url              (full SQLAlchemy URL, written by main.bicep)
//   - redis-url                 (rediss:// URL with primary key)
//   - anthropic-api-key         (placeholder; rotate to real value post-deploy)
//   - azure-openai-api-key      (placeholder; populate when AOAI resource exists)
//   - azure-openai-endpoint     (placeholder)
//   - azure-openai-deployment   (placeholder)

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object
param tenantId string = subscription().tenantId

@secure()
param postgresPassword string
@secure()
param databaseUrl string
@secure()
param redisUrl string

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

// Seed the secrets we already know at deploy time. The LLM API keys are
// placeholders — you replace them via `az keyvault secret set` after the
// initial deploy (or wire them into the bicepparam if you're comfortable
// passing them through).
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

resource sRedisUrl 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'redis-url'
  properties: { value: redisUrl }
}

resource sAnthropicKey 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: kv
  name: 'anthropic-api-key'
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

output id string = kv.id
output name string = kv.name
output uri string = kv.properties.vaultUri
