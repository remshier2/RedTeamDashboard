// Virtual network for the kit deployment.
//
// Two subnets:
//   container-apps  /23  — delegated to Microsoft.App/environments. Minimum
//                          size for a Consumption-profile Container Apps env.
//                          All kit containers egress from this subnet, giving
//                          Postgres a stable address space to allow.
//   postgres        /28  — delegated to Microsoft.DBforPostgreSQL/flexibleServers.
//                          Native VNet injection for Postgres Flexible Server
//                          (set at create time; not changeable post-deploy).

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: '${namePrefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [ '10.0.0.0/16' ] }
    subnets: [
      {
        name: 'container-apps'
        properties: {
          addressPrefix: '10.0.0.0/23'
          delegations: [
            {
              name: 'ca-delegation'
              properties: { serviceName: 'Microsoft.App/environments' }
            }
          ]
        }
      }
      {
        name: 'postgres'
        properties: {
          addressPrefix: '10.0.4.0/28'
          delegations: [
            {
              name: 'pg-delegation'
              properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' }
            }
          ]
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output containerAppsSubnetId string = vnet.properties.subnets[0].id
output postgresSubnetId string = vnet.properties.subnets[1].id
