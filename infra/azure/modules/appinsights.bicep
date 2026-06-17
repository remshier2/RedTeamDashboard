// Application Insights linked to the Log Analytics workspace.
//
// Workspace-based mode (IngestionMode: LogAnalytics) so telemetry lands in the
// same LA workspace as container logs — one query surface for everything.
// The connection string is passed to containers as
// APPLICATIONINSIGHTS_CONNECTION_STRING; the azure-monitor-opentelemetry SDK
// picks it up automatically when added to the backend/worker images.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object
param workspaceId string

resource ai 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-ai'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspaceId
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

output name string = ai.name
output connectionString string = ai.properties.ConnectionString
output instrumentationKey string = ai.properties.InstrumentationKey
