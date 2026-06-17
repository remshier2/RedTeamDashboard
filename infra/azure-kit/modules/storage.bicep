// Azure Blob Storage for engagement exports (archive / flush lifecycle).
//
// Exports land in the `engagement-exports` container as
//   {slug}/{YYYYMMDDTHHMMSSz}.json
// The container app's managed identity is granted Storage Blob Data
// Contributor by the caller (main.bicep) after both resources exist.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object

// Storage account names: 3-24 chars, lowercase alphanumeric only. We strip
// hyphens from the prefix then suffix with a unique hash so the name is
// globally unique and deterministic per resource group.
var storageAccountName = take(
  toLower(replace(namePrefix, '-', '')) + uniqueString(resourceGroup().id),
  24
)

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    accessTier: 'Cool' // write-once exports, rarely re-read
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource exportsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'engagement-exports'
  properties: { publicAccess: 'None' }
}

output storageAccountName string = storageAccount.name
output storageAccountId string = storageAccount.id
