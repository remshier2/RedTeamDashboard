// Single Container App with three colocated containers: backend, worker, redis.
//
// Why one app, not three:
//   Internal TCP routing on non-HTTP ports doesn't work in Consumption-profile
//   Container Apps envs — the env-VIP only routes 80/443. The three-app design
//   (backend + worker + a self-hosted redis app) had backend timing out
//   connecting to `redis.internal.<env>:6379`. Dedicated workload profiles fix
//   it but cost ~$130-200/mo.
//
//   For a single-user red-team tool, colocating the three containers in ONE
//   Container App lets them talk via `127.0.0.1` — no env routing involved.
//   Trade-offs: single replica (minReplicas = maxReplicas = 1), no KEDA
//   autoscaling on Redis Stream depth. Fine for one operator.
//
// Image: backend and worker share the same image; only the entrypoint
// differs. Redis is `redis:7-alpine` with persistence off.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object

param environmentId string

param keyVaultName string
param keyVaultId string

// Full image refs, e.g. `ghcr.io/donpercival0x45/rtd-backend:0.1.0`.
param backendImage string
param workerImage string

param anthropicModel string = 'claude-opus-4-7'
@allowed([ 'anthropic', 'openai', 'azure' ])
param llmProvider string = 'anthropic'

@description('Comma-separated CORS allow-origins. Add the central viewer\'s URL so the browser can call this tenant\'s backend (Phase 6).')
param corsAllowOrigins string = 'http://localhost:3001,http://127.0.0.1:3001'

@description('Entra tenant + app (client) id for analyst SSO. Blank → Entra auth stays off and the backend falls back to X-API-Key / X-User-Id. Not secret (these are public identifiers), so passed as plain env vars.')
param entraTenantId string = ''
param entraClientId string = ''

@description('Application Insights connection string. Passed to backend and worker as APPLICATIONINSIGHTS_CONNECTION_STRING.')
param appInsightsConnectionString string = ''

@description('Storage account name for engagement exports (archive/flush lifecycle). Empty → blob export disabled.')
param storageAccountName string = ''

@description('Stage 2 — base URL of the secondary MCP App (mcp_app.bicep). When acaMcpAppEnabled, Tactical stamps this onto worker envelopes for leases with requires_container=True.')
param acaMcpUrl string = ''

@description('Stage 2 — when true, leases with requires_container=True route to acaMcpUrl. False (the default) collapses every lease to the colocated /mcp on this App.')
param acaMcpAppEnabled bool = false

// ---------------------------------------------------------------------------
// Role assignment IDs
// ---------------------------------------------------------------------------

var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// ---------------------------------------------------------------------------
// Secret refs + env (one shared app, one identity)
// ---------------------------------------------------------------------------

var secretsFromKeyVault = [
  {
    name: 'database-url'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/database-url'
    identity: 'system'
  }
  {
    name: 'anthropic-api-key'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/anthropic-api-key'
    identity: 'system'
  }
  {
    name: 'openai-api-key'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/openai-api-key'
    identity: 'system'
  }
  {
    name: 'azure-openai-api-key'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/azure-openai-api-key'
    identity: 'system'
  }
  {
    name: 'azure-openai-endpoint'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/azure-openai-endpoint'
    identity: 'system'
  }
  {
    name: 'azure-openai-deployment'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/azure-openai-deployment'
    identity: 'system'
  }
  {
    // Stage 3+1: the worker hard-requires this API key — no fallback to
    // local-registry execution any more. Operator mints a cli-scoped key
    // post-deploy and populates this KV secret; until they do, the
    // worker fails fast at boot with a clear error.
    name: 'worker-mcp-api-key'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/worker-mcp-api-key'
    identity: 'system'
  }
]

// Shared between backend + worker (NOT redis — redis only needs its own env).
var appEnv = [
  { name: 'ENV', value: 'prod' }
  { name: 'DATABASE_URL', secretRef: 'database-url' }
  // Redis is a sibling container in the same pod; reachable via localhost.
  { name: 'REDIS_URL', value: 'redis://127.0.0.1:6379/0' }
  { name: 'REDIS_HOST_PORT', value: '127.0.0.1:6379' }
  { name: 'LLM_PROVIDER', value: llmProvider }
  { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
  { name: 'ANTHROPIC_MODEL', value: anthropicModel }
  { name: 'OPENAI_API_KEY', secretRef: 'openai-api-key' }
  { name: 'AZURE_OPENAI_API_KEY', secretRef: 'azure-openai-api-key' }
  { name: 'AZURE_OPENAI_ENDPOINT', secretRef: 'azure-openai-endpoint' }
  { name: 'AZURE_OPENAI_DEPLOYMENT', secretRef: 'azure-openai-deployment' }
  { name: 'AZURE_OPENAI_API_VERSION', value: '2024-08-01-preview' }
  { name: 'CORS_ALLOW_ORIGINS', value: corsAllowOrigins }
  { name: 'ENTRA_TENANT_ID', value: entraTenantId }
  { name: 'ENTRA_CLIENT_ID', value: entraClientId }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
  { name: 'AZURE_STORAGE_ACCOUNT_NAME', value: storageAccountName }
  { name: 'ACA_MCP_URL', value: acaMcpUrl }
  { name: 'ACA_MCP_APP_ENABLED', value: string(acaMcpAppEnabled) }
  { name: 'WORKER_MCP_API_KEY', secretRef: 'worker-mcp-api-key' }
]

// ---------------------------------------------------------------------------
// The one app — backend exposes external HTTPS; worker + redis are siblings
// ---------------------------------------------------------------------------

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-app'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    environmentId: environmentId
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      secrets: secretsFromKeyVault
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: backendImage
          // Run migrations before starting uvicorn. Alembic is idempotent —
          // upgrade head is a no-op if the schema is already current.
          command: [ 'sh', '-c', 'alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000' ]
          resources: { cpu: json('1'), memory: '2Gi' }
          env: appEnv
          probes: [
            {
              // Startup gives uvicorn + the DB/Redis pings time to settle
              // (Container Apps' default 1s liveness timeout kills /health
              // mid-DB-roundtrip).
              type: 'Startup'
              httpGet: { path: '/health', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 5
              timeoutSeconds: 5
              failureThreshold: 12
            }
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: 8000 }
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
        {
          name: 'worker'
          image: workerImage
          command: [ 'python', '-m', 'app.worker.main' ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: appEnv
        }
        {
          name: 'redis'
          image: 'redis:7-alpine'
          // No persistence — queue + checkpoints are ephemeral by design.
          command: [ 'redis-server', '--save', '', '--appendonly', 'no' ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
        }
      ]
      // Pinned to one replica: localhost sharing only works when backend +
      // worker + redis all live in the SAME pod. Multiple replicas each get
      // their own Redis and the queue fractures.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

resource appKvSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVaultId, app.id, 'KeyVaultSecretsUser')
  scope: resourceGroup()
  properties: {
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output appFqdn string = app.properties.configuration.ingress.fqdn
output appName string = app.name
output appPrincipalId string = app.identity.principalId
