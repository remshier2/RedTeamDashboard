// Azure Static Web App that hosts the RTD viewer for this tenant.
//
// The viewer is a pure Next.js static export (HTML+JS, no server). End
// users land at the SWA's default URL, sign in with Entra ID (gated via
// staticwebapp.config.json), then paste their backend URL + API key into
// a Source. Multi-source is preserved — one viewer can read from any
// number of RTD deployments the operator has keys for.
//
// SKU: Free tier — 100 GB bandwidth/mo, custom domain + AAD auth
// included. No real-world reason to upgrade for this use case.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object

resource swa 'Microsoft.Web/staticSites@2024-04-01' = {
  name: '${namePrefix}-viewer'
  // Static Web Apps is regional, but only some regions host it. centralus
  // works; we accept the parent module's `location` and let it fail loudly
  // if the operator chose a region SWA doesn't support.
  location: location
  tags: tags
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    // No git repo wired up — the kit's install.sh pushes the prebuilt
    // bundle to this site via the SWA deployment token after Bicep
    // returns. Skipping repository* params here means "manual deploy".
    provider: 'None'
    stagingEnvironmentPolicy: 'Disabled'
    allowConfigFileUpdates: true
  }
}

output id string = swa.id
output name string = swa.name
// The hostname includes a hash, e.g. `polite-river-12345abc.6.azurestaticapps.net`.
// install.sh prints this so the operator can hand it out + bookmark it.
output hostName string = swa.properties.defaultHostname
output url string = 'https://${swa.properties.defaultHostname}'
