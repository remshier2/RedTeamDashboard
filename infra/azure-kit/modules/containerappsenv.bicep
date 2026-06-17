// Container Apps managed environment with VNet integration.
//
// The infrastructure subnet (/23, delegated to Microsoft.App/environments)
// gives all container egress a stable address space. This is required for
// Postgres VNet injection — the server's delegated subnet only accepts
// connections from within the VNet, so the Container Apps env must be on
// the same VNet.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object

param logAnalyticsCustomerId string
@secure()
param logAnalyticsPrimarySharedKey string

@description('Resource ID of the /23 subnet delegated to Microsoft.App/environments.')
param infrastructureSubnetId string

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

output id string = env.id
output name string = env.name
output defaultDomain string = env.properties.defaultDomain
