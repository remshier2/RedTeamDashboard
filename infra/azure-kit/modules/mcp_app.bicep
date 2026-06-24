// Stage 2 — secondary Container App hosting the MCP server in isolation.
//
// Why a separate App: when a lease has ``requires_container=True``, the
// Tactical dispatcher routes the worker to this App's FQDN instead of
// the colocated /mcp on the main App. Process isolation from
// backend/worker + scale-to-zero ($0 when idle).
//
// Why not ACA Jobs: Jobs are headless — they don't accept HTTP ingress,
// which is what the SSE-based MCP transport needs. A Container App with
// scale 0..1 gives ephemeral-feeling behavior (idle replicas wind down)
// without the per-task lifecycle work.
//
// Image: reuses the backend image; entrypoint is python -m app.mcp.standalone
// (FastMCP SSE app + auth middleware, mounted at /mcp to match the
// colocated path layout).
//
// No Redis: the MCP server doesn't publish events or read inbound
// commands; that's the backend/worker contract. So this App only needs
// DATABASE_URL + PROVIDER_KEY_MASTER to validate leases and decrypt
// per-user provider keys.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object

param environmentId string

param keyVaultName string
param keyVaultId string

// Reuses the same image as backend/worker; only the command differs.
param backendImage string

@description('Application Insights connection string. Same DSN as the main App so traces stitch.')
param appInsightsConnectionString string = ''

// ---------------------------------------------------------------------------
// Role assignment IDs
// ---------------------------------------------------------------------------

var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// ---------------------------------------------------------------------------
// Secret refs + env
// ---------------------------------------------------------------------------

var secretsFromKeyVault = [
  {
    name: 'database-url'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/database-url'
    identity: 'system'
  }
  {
    name: 'provider-key-master'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/provider-key-master'
    identity: 'system'
  }
]

var appEnv = [
  { name: 'ENV', value: 'prod' }
  { name: 'DATABASE_URL', secretRef: 'database-url' }
  { name: 'PROVIDER_KEY_MASTER', secretRef: 'provider-key-master' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
]

// ---------------------------------------------------------------------------
// The MCP host App — scale 0..1
// ---------------------------------------------------------------------------

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-mcp'
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
          name: 'mcp'
          image: backendImage
          command: [ 'python', '-m', 'app.mcp.standalone' ]
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: appEnv
        }
      ]
      // Scale-to-zero: idle is $0, ramps to 1 on first request. Single
      // replica cap keeps lease state simple (no inter-replica sync;
      // every lease lookup hits the shared Postgres anyway).
      scale: {
        minReplicas: 0
        maxReplicas: 1
        rules: [
          {
            name: 'http-rule'
            http: { metadata: { concurrentRequests: '10' } }
          }
        ]
      }
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

output appName string = app.name
output appFqdn string = app.properties.configuration.ingress.fqdn
output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output appPrincipalId string = app.identity.principalId
