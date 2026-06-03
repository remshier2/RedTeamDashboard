// Example parameters for `az deployment sub create` against main.bicep.
//
// The installer (scripts/install.sh) fills these in interactively and runs
// the deploy for you. Edit this file only if you want to drive the deploy
// directly with `az deployment sub create --parameters @main.bicepparam`.

using './main.bicep'

param env = 'prod'
param location = 'eastus2'

// Resource group name defaults to rtd-<env>. Uncomment to override.
// param resourceGroupName = 'rtd-prod'

param postgresAdminLogin = 'rtdadmin'

// Never commit this with a real value. The installer prompts for it and
// passes it inline via `--parameters postgresAdminPassword=$PG_PW`.
// param postgresAdminPassword = ''

// Image source — GHCR. Override `imageRepoOwner` if you forked the repo and
// publish images under your own account.
param imageRepoOwner = 'donpercival0x45'
param imageTag = 'latest'

// Default model provider for runs that don't specify one. Per-run override
// (via the CLI / API) always wins, so this is just the floor default.
param llmProvider = 'anthropic'
param anthropicModel = 'claude-opus-4-7'

// Extra CORS allow-origins. The in-tenant Static Web App viewer URL is
// auto-appended by main.bicep — set this ONLY if you want additional
// origins (e.g. a self-hosted viewer at a custom domain). Comma-separated.
param extraCorsAllowOrigins = 'http://localhost:3001,http://127.0.0.1:3001'
