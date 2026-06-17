// Container Apps Environment + 3 apps (backend, worker, frontend).
//
// - Environment: Consumption-profile, VNet-integrated. The /23 infrastructure
//   subnet gives containers a stable egress address space so Postgres (VNet-
//   injected, no public access) can accept connections from within the VNet.
//   Logs ship to the Log Analytics workspace.
// - backend:  external ingress on 8000 -> 443. Pulls from ACR via system
//             identity; reads secrets from Key Vault via system identity.
// - worker:   no ingress. Same image, different entrypoint. Scales 1-3
//             on Redis Stream depth (KEDA scaler defined inline).
// - frontend: external ingress on 3000 -> 443. Same KV+ACR setup.
//
// First deploy will create the apps in a failed state because the ACR images
// don't exist yet. Push images, then re-run this template (or update each
// app's `image` property) to roll the new revision.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object

param logAnalyticsCustomerId string
@secure()
param logAnalyticsPrimarySharedKey string

param acrLoginServer string
param acrId string

param keyVaultName string
param keyVaultId string

param backendImage string
param workerImage string
param frontendImage string

param anthropicModel string = 'claude-opus-4-7'
param llmProvider string = 'anthropic'

@description('Application Insights connection string. Passed to backend and worker as APPLICATIONINSIGHTS_CONNECTION_STRING.')
param appInsightsConnectionString string = ''

@description('Resource ID of the /23 subnet delegated to Microsoft.App/environments.')
param infrastructureSubnetId string

// ---------------------------------------------------------------------------
// Managed environment
// ---------------------------------------------------------------------------

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-cae'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsPrimarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    }
    zoneRedundant: false
  }
}

// ---------------------------------------------------------------------------
// Role assignments — granted to each app's system identity post-creation
// ---------------------------------------------------------------------------

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

var secretsFromKeyVault = [
  {
    name: 'database-url'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/database-url'
    identity: 'system'
  }
  {
    name: 'redis-url'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/redis-url'
    identity: 'system'
  }
  {
    name: 'anthropic-api-key'
    keyVaultUrl: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/secrets/anthropic-api-key'
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
]

var backendEnv = [
  { name: 'ENV', value: 'prod' }
  { name: 'DATABASE_URL', secretRef: 'database-url' }
  { name: 'REDIS_URL', secretRef: 'redis-url' }
  { name: 'LLM_PROVIDER', value: llmProvider }
  { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
  { name: 'ANTHROPIC_MODEL', value: anthropicModel }
  { name: 'AZURE_OPENAI_API_KEY', secretRef: 'azure-openai-api-key' }
  { name: 'AZURE_OPENAI_ENDPOINT', secretRef: 'azure-openai-endpoint' }
  { name: 'AZURE_OPENAI_DEPLOYMENT', secretRef: 'azure-openai-deployment' }
  { name: 'AZURE_OPENAI_API_VERSION', value: '2024-08-01-preview' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
]

// ---------------------------------------------------------------------------
// Backend
// ---------------------------------------------------------------------------

resource backend 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-backend'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    environmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        { server: acrLoginServer, identity: 'system' }
      ]
      secrets: secretsFromKeyVault
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: backendImage
          resources: { cpu: json('1'), memory: '2Gi' }
          env: backendEnv
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: 8000 }
              periodSeconds: 30
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

resource backendAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acrId, backend.id, 'AcrPull')
  scope: resourceGroup()
  properties: {
    principalId: backend.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource backendKvSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVaultId, backend.id, 'KeyVaultSecretsUser')
  scope: resourceGroup()
  properties: {
    principalId: backend.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

// ---------------------------------------------------------------------------
// Worker — no ingress, scales on Redis Stream depth
// ---------------------------------------------------------------------------

resource worker 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-worker'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    environmentId: env.id
    configuration: {
      registries: [
        { server: acrLoginServer, identity: 'system' }
      ]
      secrets: secretsFromKeyVault
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: workerImage
          command: [ 'python', '-m', 'app.worker.main' ]
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: backendEnv
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'redis-stream-depth'
            custom: {
              type: 'redis-streams'
              metadata: {
                addressFromEnv: 'REDIS_URL'
                stream: 'runs:in'
                consumerGroup: 'osint-workers'
                pendingEntriesCount: '5'
              }
            }
          }
        ]
      }
    }
  }
}

resource workerAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acrId, worker.id, 'AcrPull')
  scope: resourceGroup()
  properties: {
    principalId: worker.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource workerKvSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVaultId, worker.id, 'KeyVaultSecretsUser')
  scope: resourceGroup()
  properties: {
    principalId: worker.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

// ---------------------------------------------------------------------------
// Frontend
// ---------------------------------------------------------------------------

resource frontend 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-frontend'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    environmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        { server: acrLoginServer, identity: 'system' }
      ]
    }
    template: {
      containers: [
        {
          name: 'frontend'
          image: frontendImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'NODE_ENV', value: 'production' }
            // NEXT_PUBLIC_API_BASE is baked at image build; see README runbook.
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
}

resource frontendAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acrId, frontend.id, 'AcrPull')
  scope: resourceGroup()
  properties: {
    principalId: frontend.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output environmentId string = env.id
output backendFqdn string = backend.properties.configuration.ingress.fqdn
output frontendFqdn string = frontend.properties.configuration.ingress.fqdn
output workerName string = worker.name
