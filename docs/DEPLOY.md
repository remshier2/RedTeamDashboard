<!--
RedTeamDashboard — Defensive Security Operations and Governance Platform

This documentation describes a platform for managing authorized security engagements.
All work described is conducted with explicit approval and scope boundaries.

Charter:
- Agents perform enumeration and scanning only
- Validation/proof-of-concept work is analyst-only
- All actions are approval-gated and audit-logged

Terminology Note: "exploit" in this context refers to validation/proof-of-concept
work conducted by analysts during authorized engagements, not unauthorized intrusion.
-->

# Red Team Dashboard — Deploy & Operate

How the system is wired, how to stand up a fresh environment in your own
Azure tenant, and how to use it day-to-day.

## Architecture

```
Operator / teammate
├─ rtd-cli                ──── HTTPS+X-API-Key ───┐
└─ Browser → SWA viewer ──┐                       │
   (Entra-gated)          │                       │
                          ▼                       ▼
   ┌──────────────────────────────────────────────────────┐
   │  Azure RG: rtd-<env>                                 │
   │                                                      │
   │  Static Web App: rtd-<env>-viewer (Free SKU)         │
   │   serves the Next.js static bundle                   │
   │                          │                           │
   │   reads from ┄┄┄┄┄┄┄┄┄┄┄┘                           │
   │                                                      │
   │  VNet: 10.0.0.0/16                                   │
   │  ┌─────────────────────────────────────────────┐     │
   │  │ Subnet: container-apps  10.0.0.0/23         │     │
   │  │  Container Apps Env (Consumption)            │     │
   │  │  Container App: rtd-<env>-app                │     │
   │  │  ┌──────────┐ ┌────────┐ ┌────────────┐     │     │
   │  │  │ backend  │ │ worker │ │ redis:7    │     │     │
   │  │  │ uvicorn  │ │ lang   │ │ localhost  │     │     │
   │  │  │ :8000    │ │ graph  │ │ :6379      │     │     │
   │  │  └────┬─────┘ └───┬────┘ └─────┬──────┘     │     │
   │  │       │    127.0.0.1            │            │     │
   │  └───────┼─────────────────────────────────────┘     │
   │          │                                            │
   │  ┌───────┼─────────────────────────────────────┐     │
   │  │ Subnet: postgres  10.0.4.0/28               │     │
   │  │  Postgres Flexible Server (private only)    │     │
   │  └─────────────────────────────────────────────┘     │
   │                                                      │
   │  Key Vault (RBAC)   Log Analytics   App Insights     │
   │  pg pw, db url,     container logs  traces + errors  │
   │  LLM keys, api key                                   │
   └──────────────────────────────────────────────────────┘
```

**Why one app with three containers:** Container Apps' internal env VIP
only routes 80/443 — it can't carry TCP/6379 between sibling apps. Colocation
means backend/worker/redis talk via `127.0.0.1`. Trade-off: single replica only.

**Postgres network model:** The Postgres server is VNet-injected into a
dedicated /28 subnet with `publicNetworkAccess: Disabled`. It is unreachable
from the internet; only traffic from within the VNet (the Container Apps
subnet) can connect. A private DNS zone resolves the server hostname inside
the VNet.

## Prereqs (one-time on your machine)

```bash
az --version              # any 2.50+
az bicep install
az login
az account set --subscription <your-sub>
openssl version           # for postgres password generation
python3 --version         # for JSON parsing in install.sh
docker --version          # for building + deploying the viewer bundle
```

## Deploy a fresh environment

```bash
git clone https://github.com/DonPercival0x45/RedTeamDashboard.git
cd RedTeamDashboard

./infra/azure-kit/scripts/install.sh \
    --env prod \
    --location centralus \
    --anthropic-key sk-ant-… \
    --yes
```

That's it. The script handles everything end-to-end in ~10 minutes.

**What happens:**

1. Generates a random Postgres admin password
2. Runs the Bicep deploy — VNet, Postgres (private), Key Vault, Log Analytics,
   App Insights, Container Apps env, the one Container App, Static Web App
3. Forces a fresh revision to clear the KV identity propagation race on first deploy
4. Polls `/health` every 6s — the backend runs `alembic upgrade head` before
   uvicorn starts, so the schema is initialized by the time health turns green
5. Builds the Next.js viewer in Docker with this deployment's API URL baked in
   and pushes it to the Static Web App
6. Grants your user Key Vault Secrets Officer, mints the bootstrap admin API key,
   prompts you to paste it back so it's stored in KV, stores your LLM key(s)
7. Restarts the app to pick up the newly stored KV secrets
8. Prints the viewer URL and quick-start link for your teammate

**Flags:**

| Flag | Default | Notes |
|---|---|---|
| `--env` | `prod` | Prefix for all resource names |
| `--location` | `eastus2` | Use `centralus` on PAYG/personal subs |
| `--image-tag` | `latest` | Pin a version in production |
| `--anthropic-key` | `$ANTHROPIC_API_KEY` | Stored in KV; prompted if missing |
| `--openai-key` | `$OPENAI_API_KEY` | Stored in KV if provided |
| `--llm-provider` | `anthropic` | `anthropic \| openai \| azure` |
| `--entra-tenant-id` | *(blank)* | Enables per-analyst SSO |
| `--entra-client-id` | *(blank)* | Required with `--entra-tenant-id` |
| `--yes` | *(interactive)* | Skip confirmation prompt |

## Day-to-day operation

### CLI

```bash
pip install -e ./cli
rtd --version

rtd login --profile prod \
  --url https://<app-fqdn>.azurecontainerapps.io \
  --key rtd_yourtoken \
  --default

rtd engagement create "Acme Q3 Pentest"
rtd engagement scope add acme-q3-pentest --kind domain --value acme.com
rtd run start acme-q3-pentest -p "Run passive OSINT on acme.com" --tail

rtd approve <approval-id>
rtd approve <approval-id> --remember   # creates a session grant
rtd approve <approval-id> --deny --reason "out of scope"

rtd findings list acme-q3-pentest --severity high
rtd tail acme-q3-pentest

# Add a freeform observation (doesn't need validation)
rtd engagement observations add acme-q3-pentest "Login portal exposes version string in Server header" --phase osint
rtd engagement observations list acme-q3-pentest

# Bulk import findings from a prior report or scanner output
# FILE is a JSON array — each object needs at minimum: title
# Optional: severity, phase, summary, target, source_tool, details
rtd engagement import-findings acme-q3-pentest findings.json
```

**findings.json shape for import:**

```json
[
  {
    "title": "TLS certificate expires in 14 days",
    "severity": "medium",
    "phase": "osint",
    "target": "acme.com",
    "summary": "Certificate issued by Let's Encrypt, expires 2026-07-01.",
    "source_tool": "manual"
  },
  {
    "title": "Subdomain takeover candidate",
    "severity": "high",
    "phase": "osint",
    "target": "staging.acme.com",
    "source_tool": "subfinder"
  }
]
```

All imported findings land as `pending_validation` and appear in the viewer for analyst review before they become report-eligible.

### Browser viewer

The viewer URL is printed at the end of `install.sh`. Share the quick-start
link with your teammate — it pre-fills the backend URL so they only need to
paste their API key.

**Minting scoped keys for analysts:**

```bash
# Full control (create engagements, run OSINT, approve tools)
az containerapp exec -n rtd-prod-app -g rtd-prod --container backend \
    --command 'python -m app.scripts.mint_api_key --name alice --scope cli'

# Read-only (browse findings, download reports — no write buttons in UI)
az containerapp exec -n rtd-prod-app -g rtd-prod --container backend \
    --command 'python -m app.scripts.mint_api_key --name bob-readonly --scope viewer'
```

## Operations

```bash
# Tail container logs
az containerapp logs show -n rtd-prod-app -g rtd-prod \
    --container backend --tail 60 --format text

# Restart after rotating a KV secret
REV=$(az containerapp revision list -n rtd-prod-app -g rtd-prod \
    --query '[?properties.active].name | [0]' -o tsv)
az containerapp revision restart -n rtd-prod-app -g rtd-prod --revision "$REV"

# Roll to a new image (re-running install is idempotent)
./infra/azure-kit/scripts/install.sh --env prod --location centralus \
    --image-tag v0.2.0 --yes

# Tear everything down
az group delete -n rtd-prod -y
```

## Deploy from GitHub (on-demand)

The `Deploy` workflow at `.github/workflows/deploy.yml` is a manual-trigger
pipeline: it builds the current `main` HEAD, pushes the image to GHCR
tagged `:main-<short-sha>`, and rolls the Container App's backend + worker
containers via two `az containerapp update` calls. No `on: push` — the
workflow only fires when *you* run it.

**One-time setup** (after `install.sh`):

```bash
export GITHUB_OWNER=DonPercival0x45
export GITHUB_REPO=RedTeamDashboard
export AZURE_RG=rtd-prod
export AZURE_APP=rtd-prod-app
./infra/azure-kit/scripts/setup-github-deploy.sh
```

This creates an Entra app registration with a federated credential trusting
the main branch via OIDC, grants `Container Apps Contributor` on the
resource group, and writes the five `AZURE_*` repo variables. No long-lived
service principal secret — GitHub Actions exchanges its OIDC token for an
Azure access token at run time.

**Trigger a deploy:**

```bash
gh workflow run deploy.yml
# or: GitHub UI -> Actions -> Deploy -> "Run workflow"
```

**Rollback to the prior revision:**

```bash
az containerapp revision list -n rtd-prod-app -g rtd-prod -o table
# Pick the prior revision name, then:
az containerapp revision activate -n rtd-prod-app -g rtd-prod --revision <name>
```

## Entra ID SSO (optional)

Run `setup-entra.sh` before `install.sh` to create the app registration, then
pass the IDs to the installer:

```bash
./infra/azure-kit/scripts/setup-entra.sh \
    --env prod \
    --viewer-url https://<viewer>.azurestaticapps.net

./infra/azure-kit/scripts/install.sh \
    --env prod \
    --location centralus \
    --entra-tenant-id <tenant-id> \
    --entra-client-id <client-id> \
    --anthropic-key sk-ant-… \
    --yes
```

See `docs/ENTRA_SETUP.md` for the full walkthrough.

## MCP server (Claude Code)

The backend exposes an MCP server at `/mcp/sse`. Any MCP-compatible agent can connect to it using an RTD API key — Claude Code is the primary intended client.

**Connect Claude Code:**

```bash
claude mcp add rtd-prod \
    --transport sse \
    --url https://<app-fqdn>.azurecontainerapps.io/mcp/sse \
    --header 'X-API-Key: rtd_yourtoken'
```

The install script prints this exact command (with the FQDN filled in) at the end of step 6.

**Two orchestration modes:**

| Mode | How | LLM cost | Analyst in loop |
|---|---|---|---|
| Autonomous | `rtd run start <slug> -p "..."` | Anthropic API key | No |
| Interactive | Claude Code + MCP | Claude Max subscription | Yes |

Both modes write findings to the same database. The viewer shows results from either.

**What the MCP server exposes:**

- *Passive tools* (dns_lookup, whois_lookup, crt_sh, httpx_probe, subfinder, reverse_dns) — run freely, scope-checked server-side
- *Active tools* (port_scan, subnet_sweep, service_detect) — Claude Code asks the analyst before calling
- *Engagement tools* (list/create engagements, get/add scope, list/create findings)
- *Lifecycle tools* (export_engagement, archive_engagement, flush_engagement_data)
- *Resources* (engagements://list, engagement://{slug}, findings://{slug}) — for agent context
- *Prompts* (passive_recon, active_enum, deep_dive) — structured workflow templates

**Authentication:** every request to `/mcp/*` requires `X-API-Key`. A `cli` scoped key covers OSINT tools and findings; `admin` scope is required for archive and flush.

**Worker MCP key (REQUIRED — Stage 3+1):** the worker calls the MCP server over SSE on every run using `WORKER_MCP_API_KEY` + the per-run `X-Lease-Token`. Mint a `cli`-scoped key once per deployment and set it in env (Bicep: read from Key Vault secret `worker-mcp-api-key`). **The Stage 1.5 local-execution fallback was ripped** — when `WORKER_MCP_API_KEY` is blank the worker now fails fast at boot with a clear message rather than silently running tools against the local registry. Tactical-dispatched runs and direct `POST /engagements/{slug}/runs` runs both mint leases before the envelope hits Redis; the worker hard-requires both `mcp_url` and `lease_token` on every envelope.

**Isolated MCP host (Stage 2):** the deploy kit also provisions a second Container App, `rtd-<env>-mcp`, running the MCP server in isolation from backend/worker. Its ingress runs the same `/mcp` path as the colocated one, but the App scales 0..1 — idle = $0, ramps to 1 on first request. When `mcp_leases.requires_container=True` AND `ACA_MCP_APP_ENABLED=true` AND `ACA_MCP_URL` is populated, Tactical stamps the secondary App's URL onto the worker envelope; otherwise the run uses the colocated path.

**Strategic policy LLM (Stage 3):** every `provision_lease` call fires one Strategic LLM call (via `with_structured_output(_LeasePolicy)`) to decide (a) which subset of the pack-default tool list this run actually needs and (b) whether the run should route to the isolated MCP host. The decision lands on a new `AgentExecution(trigger=lease_provision)` row so the Costs tab attributes the spend per-lease. The narrow-only enforcement is server-side — even if the LLM tries to widen past pack defaults or include an exploit tool, the result is filtered before the lease persists. The dispatch tool is always preserved so the worker can execute the task. Failure modes (no provider key for the engagement creator, LLM error, structured-output validation error) are caught: the row is marked `status=failed` with the error string and the lease falls back to pack defaults + `requires_container=False`, so dispatch is never blocked by Strategic. BYO-key posture: Strategic uses the engagement creator's stored provider key, same as `analyze_finding` — no env fallback by design.

**Lease sweeper:** the worker runs a daemon thread that calls `mcp_lease.sweep_expired()` every `LEASE_SWEEP_INTERVAL` seconds (default 300s) to flip active leases past their `expires_at` to `status=expired`. Per-request `validate_token` already rejects expired leases at the MCP server, so this is purely for accounting cleanliness — the Costs UI and lease-state queries don't accumulate stale "active" rows. Failures on a single tick are logged and swallowed; the next tick gets a fresh shot.

**Nessus import (Phase 10):** Tenable Nessus is a "heavy tool" per charter §16 RESOLVED — import-first, not agent-driven. Analysts run scans on their own infra and `POST /engagements/{slug}/findings/import/nessus` the `.nessus` v2 XML export (multipart `file=`). Each ReportItem becomes a Finding with `phase=vuln_scan`, `source_tool="nessus_import"`, `status=pending_validation` (Phase 8 validation gate applies). Severity maps 0→info, 1→low, 2→medium, 3→high, 4→critical. By default Severity=Info findings are skipped (`?include_info=true` opts in); out-of-scope hosts are dropped silently and counted on the response. Uses `defusedxml` for XXE/billion-laughs safety.

**Workflow templates (Phase 10):** reusable starter packs the analyst applies to an engagement with one target to mint N pending Tasks at once. The `is_system=true` set (Network Recon, OSINT Enum, Web App per charter §16 RESOLVED) is code-seeded idempotently on backend startup — the seed function does not mutate existing system rows even if the code constant changes, so in-flight engagements keep stable template shapes. Apply via `POST /engagements/{slug}/templates/{id}/apply` with `{target}`: created Tasks land `status=pending`, analyst still has to dispatch each via the existing Tactical path (matches the Strategic-suggestion posture: suggest, don't auto-run). Charter defense-in-depth in `apply_template` refuses exploit-kind steps even if a user template tried to embed one. UI lives under the new "Templates" tab in the engagement left-nav; analyst-authored template CRUD (`is_system=false`) is deferred to a follow-on PR.

## Engagement lifecycle

Engagements move through three states: **active → archived → flushed**.

```
active   → archived   → flushed
(visible)  (hidden)     (gone from DB, blob only)
```

**Archive** — marks an engagement done. Stays in the database but is excluded from active views. An export JSON is uploaded to blob storage first.

```bash
rtd engagement archive acme-q3          # requires admin key
# or via MCP: archive_engagement("acme-q3")
```

**Flush** — permanently deletes all engagement data from the database: findings, scope, approvals, and audit logs. Export is uploaded to blob first. Cannot be undone.

```bash
rtd engagement flush acme-q3            # prompts for confirmation
rtd engagement flush acme-q3 --yes      # skip prompt (scripts)
# or via MCP: flush_engagement_data("acme-q3", confirmed=True)
```

**Export only** — upload a snapshot to blob without changing status. Useful for point-in-time backups mid-engagement.

```bash
# CLI: use the API directly
curl -X POST https://<fqdn>/engagements/acme-q3/export \
    -H "X-API-Key: rtd_admintoken"
# or via MCP: export_engagement("acme-q3")
```

**Blob storage:** exports land at
```
https://<storage-account>.blob.core.windows.net/engagement-exports/<slug>/<YYYYMMDDTHHMMSSz>.json
```

The storage account name is printed at the end of `install.sh` and in the Bicep outputs (`storageAccountName`). The container app's managed identity has `Storage Blob Data Contributor` — no connection string needed.

**Typical quarterly rhythm:**
1. Create engagement, add scope, run recon over 1-2 months
2. Write the report from validated findings
3. `rtd engagement archive <slug>` — export + hide from viewer
4. Start next engagement; old data is safely in blob if you ever need it
5. `rtd engagement flush <slug>` — once you're confident the blob is sufficient

## Expected costs

| Resource | ~Monthly |
|---|---|
| Postgres Flexible Server B1ms | $13 |
| Container App (1.5 vCPU / 3 GiB, 1 replica) | $15–20 |
| Key Vault Standard | <$1 |
| Log Analytics (low ingest) | $1–5 |
| App Insights (first 5 GB free) | $0–2 |
| Blob Storage LRS Cool (engagement exports) | <$1 |
| VNet, DNS zone, SWA Free | $0 |
| **Total** | **~$30–41/mo** |

## Things to know

- **Single replica is non-negotiable** with this architecture. Outgrowing it
  means adding Azure Managed Redis as a separate resource (~$50/mo) and
  raising `minReplicas`.
- **Redis has no persistence** (`--save '' --appendonly no`). A container
  restart drops the job queue. In-progress LangGraph runs survive because
  checkpoints are in Postgres.
- **Alembic runs automatically** on every container start (`alembic upgrade head`
  before uvicorn). It is idempotent — a no-op if the schema is current. No
  manual migration step is needed.
- **Postgres is private.** There is no public endpoint. The Container Apps
  environment is on the same VNet and resolves the hostname via the private
  DNS zone. The Azure portal's query editor will not reach it — use
  `az containerapp exec` to run psql inside the backend container if needed.
- **CI does not deploy.** Releases tag GHCR images; you run the kit yourself.
- **Default region is `eastus2`** but PAYG/personal subs reject Postgres there.
  Use `--location centralus` until on an EA/CSP sub.
