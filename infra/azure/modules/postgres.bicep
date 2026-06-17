// Postgres Flexible Server — hosts the app schema (engagements, scope,
// findings, approvals, audit_log) plus LangGraph's checkpoint_* tables.
//
// Burstable B1ms (~$13/mo). SSL is forced.
//
// Network model: native VNet injection via a delegated /28 subnet. The server
// gets a private IP inside the VNet and is unreachable from the public
// internet (publicNetworkAccess: Disabled). Container Apps egress through
// the same VNet so they can reach the private IP. DNS resolution inside the
// VNet uses the private DNS zone provisioned in main.bicep.
//
// NOTE: delegatedSubnetResourceId and privateDnsZoneArmResourceId must be
// set at creation time — they cannot be changed after the server is deployed.
// Recreate the server (and restore from backup) if the network config needs
// to change.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object
param postgresVersion string = '16'
param skuName string = 'Standard_B1ms'
param storageSizeGB int = 32
param adminLogin string

@secure()
param adminPassword string

@description('Resource ID of the /28 subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers.')
param delegatedSubnetId string

@description('Resource ID of the private DNS zone (privatelink.postgres.database.azure.com) linked to the VNet.')
param privateDnsZoneId string

var serverName = '${namePrefix}-pg'
var databaseName = 'rtd'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: 'Burstable'
  }
  properties: {
    version: postgresVersion
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    storage: {
      storageSizeGB: storageSizeGB
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      delegatedSubnetResourceId: delegatedSubnetId
      privateDnsZoneArmResourceId: privateDnsZoneId
      publicNetworkAccess: 'Disabled'
    }
  }
}

resource db 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

output id string = server.id
output name string = server.name
output fqdn string = server.properties.fullyQualifiedDomainName
output databaseName string = databaseName
@secure()
output sqlAlchemyUrl string = 'postgresql+psycopg://${adminLogin}:${adminPassword}@${server.properties.fullyQualifiedDomainName}:5432/${databaseName}?sslmode=require'
