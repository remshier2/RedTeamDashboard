// Azure Container Registry — image store for CI/CD push and Container App pull.
//
// Standard SKU: minimum required for webhooks. Basic lacks webhook support;
// Premium adds geo-replication and private endpoints (overkill for a
// single-operator tool).
//
// Security posture: admin user disabled — all access via RBAC/managed identity.
//   - CI/CD (GH Actions): AcrPush scoped to this registry only (setup-github-deploy.sh).
//   - Container Apps: AcrPull via their system-assigned managed identities
//     (role assignments live in containerapps.bicep and mcp_app.bicep, next to
//     the identities they grant, to avoid a circular dependency here).
//   - ACR Task (deploy-rtd): Container Apps Contributor granted in setup-acr-deploy.sh.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  // ACR names are globally unique and alphanumeric only (no hyphens).
  name: replace('${namePrefix}acr', '-', '')
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    // Allow Azure services (ACR Tasks, Container Apps) to reach the registry
    // without additional network rules.
    networkRuleBypassOptions: 'AzureServices'
    policies: {
      retentionPolicy: { status: 'enabled', days: 7 }
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output acrId string = acr.id
