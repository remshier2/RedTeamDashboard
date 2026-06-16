# Red Team Dashboard — Deploy & Operate

How the system is wired, how to stand up a fresh environment in your own
Azure tenant, and how to use it day-to-day. Verified end-to-end on
2026-06-03 against `rtd-personal` / `centralus`.

## Architecture in one picture

```
Operator / teammate
├─ rtd-cli                ──── HTTPS+X-API-Key ───┐
└─ Browser → SWA viewer ──┐                       │
   (Entra-gated)          │                       │
                          ▼                       ▼
   ┌──────────────────────────────────────────────────────┐
   │  Azure RG: rtd-<env>  (one per deployment)           │
   │                                                      │
   │  Static Web App: rtd-<env>-viewer                    │
   │   Entra ID gated · serves the Next.js static bundle  │
   │                          │                           │
   │   reads from ┄┄┄┄┄┄┄┄┄┄┄┄┘                           │
   │                                                      │
   │  Container App: rtd-<env>-app                        │
   │  ┌─────────┐  ┌─────────┐  ┌──────────────┐          │
   │  │ backend │  │ worker  │  │ redis:7      │          │
   │  │ uvicorn │  │ langgr. │  │ localhost    │          │
   │  │ :8000   │  │         │  │ :6379        │          │
   │  └────┬────┘  └────┬────┘  └──────┬───────┘          │
   │       │ 127.0.0.1 between all three                  │
   │       │                                              │
   │       ▼               ▼                              │
   │  Postgres FS     Key Vault (RBAC)                    │
   │  (public + AZ    pg pw, db url,                      │
   │   firewall rule) LLM keys, admin key                 │
   │                                                      │
   │  Log Analytics  +  CAE (no VNet)                     │
   └──────────────────────────────────────────────────────┘
```

**Why one app with three containers:** Container Apps' internal env VIP
only routes 80/443 — it can't carry TCP/6379 between sibling apps, even
with VNet integration. Colocation in the same pod means
backend/worker/redis talk via `127.0.0.1` and never need env routing. The
trade-off: single replica only.

## Prereqs (one-time on your machine)

```bash
# Azure CLI + Bicep
az --version              # any 2.50+
az bicep install
az login                  # opens browser
az account set --subscription rtd-personal   # whatever your sub is named

# Other tools
openssl version           # for the install script to generate the pg pw
python3 --version         # install.sh uses python3 for JSON parsing
gh --version              # only needed if you'll cut releases
node --version            # 18+; needed for the SWA CLI viewer deploy step
                          # (npx fetches @azure/static-web-apps-cli on first run)

# Pin your subscription so you don't accidentally deploy elsewhere
az account show --query 'name'
```

## Deploy a fresh environment in ~8 minutes

```bash
git clone https://github.com/DonPercival0x45/RedTeamDashboard.git
cd RedTeamDashboard

./infra/azure-kit/scripts/install.sh --env prod --location centralus --yes
```

That's the whole thing. Flags:

- `--env <name>` — becomes the resource prefix (`rtd-<env>-app`,
  `rtd-<env>-kv`, RG `rtd-<env>`). Defaults to `prod`.
- `--location centralus` — required on Pay-As-You-Go / personal subs.
  `eastus2` and `eastus` hit `LocationIsOfferRestricted` on Postgres
  Flexible Server.
- `--image-tag latest` — defaults to `:latest`, pulls
  `ghcr.io/donpercival0x45/rtd-{backend,worker}:latest`. Pin a version
  in production.
- `--image-repo-owner <gh-user>` — override if you forked and republish.
- `--entra-tenant-id <id>` / `--entra-client-id <id>` — enable per-analyst
  Entra SSO (from `setup-entra.sh`; see `docs/ENTRA_SETUP.md`). Both set →
  the backend validates SSO tokens and the viewer is built with sign-in on.
  Omit → API-key auth only. The viewer is built from your checkout at install
  time (in Docker) so this tenant's API URL + Entra IDs are baked into the
  static bundle.
- `--yes` skips the confirmation prompt.

What it does (~8 min wall-clock):

1. Generates a random Postgres admin password.
2. `az deployment sub create` against `infra/azure-kit/main.bicep` —
   provisions RG, Log Analytics, Postgres Flexible Server (Burstable B1ms,
   public + AllowAzureServices firewall), Container Apps Env
   (Consumption-only), Key Vault (RBAC mode), and the one Container App
   with three containers.
3. Forces a fresh revision — the first revision races KV→AAD identity
   propagation and KV refs return 403; bumping makes the second revision
   pick up the now-propagated identity.
4. Curls `/health` every 6s for 240s — exits 0 when
   `{"db":true,"redis":true}`.
5. Prints the manual bootstrap commands.

## Bootstrap (4 paste-able command groups)

The install script prints these at the end with the right names filled in.
The pattern:

```bash
# 1. Apply migrations (3 of them; idempotent)
script -qc "az containerapp exec \
    -n rtd-prod-app -g rtd-prod --container backend \
    --command 'alembic upgrade head'" /dev/null

# 2. Mint your bootstrap admin key — COPY THE rtd_… TOKEN. It cannot be
#    retrieved again.
script -qc "az containerapp exec \
    -n rtd-prod-app -g rtd-prod --container backend \
    --command 'python -m app.scripts.mint_api_key --name bootstrap --scope admin'" /dev/null

# 3. One-time per RG: grant YOURSELF data-plane access to the new Key Vault.
#    (Subscription Owner doesn't auto-inherit KV data-plane in RBAC mode.)
ME=$(az ad signed-in-user show --query id -o tsv)
KV_ID=$(az keyvault show -n rtd-prod-kv --query id -o tsv)
az role assignment create --role "Key Vault Secrets Officer" --assignee "$ME" --scope "$KV_ID"
sleep 60  # AAD propagation

# 4. Stash the admin key + your LLM key(s) in KV
az keyvault secret set --vault-name rtd-prod-kv --name admin-api-key      --value 'rtd_…paste…'
az keyvault secret set --vault-name rtd-prod-kv --name anthropic-api-key  --value 'sk-ant-…'

# 5. Restart so the new LLM key gets picked up
REV=$(az containerapp revision list -n rtd-prod-app -g rtd-prod \
    --query '[?properties.active].name | [0]' -o tsv)
az containerapp revision restart -n rtd-prod-app -g rtd-prod --revision "$REV"
```

The `script -qc "..." /dev/null` wrapper gives `az containerapp exec` a
TTY — without it you get `Inappropriate ioctl for device`.

After step 5, `/health` is green and the worker has a valid LLM key.
Operational.

## Day-to-day operation

### CLI (preferred for mutations)

The CLI isn't on PyPI yet (Trusted Publisher setup pending). Install from
source:

```bash
pip install -e ./cli   # or: pipx install ./cli
rtd --version
```

```bash
# Save a profile (URL + API key) — auto-persists to ~/.config/rtd/config.toml (0600)
rtd login --profile prod \
  --url https://rtd-prod-app.<env-suffix>.<region>.azurecontainerapps.io \
  --key rtd_yourtoken \
  --default

# Day-to-day flow
rtd engagement create "Acme Q3 Pentest"
rtd engagement scope add acme-q3-pentest --kind domain --value acme.com
rtd run start acme-q3-pentest -p "Run passive OSINT on acme.com" --tail
#  ↑ --tail streams SSE events to your terminal until the run completes

# When an active-tool approval prompt arrives:
rtd approve <approval-id>             # approve as-is
rtd approve <approval-id> --remember  # approve + create a session grant
rtd approve <approval-id> --deny --reason "out of scope"
rtd approve <approval-id> --edit port=8443  # edit args then approve

rtd grants list acme-q3-pentest       # see active session grants
rtd grants revoke <grant-id>          # revoke one

rtd findings list acme-q3-pentest --severity high
rtd tail acme-q3-pentest              # late-join an in-flight stream
```

### Viewer (browser GUI — full CLI parity)

The kit provisions an **Azure Static Web App (Free SKU)** in your RG and
pushes the viewer bundle to it. The viewer's URL is printed at the end of
`install.sh`:

```
Viewer URL:  https://rtd-<env>-viewer-<hash>.<region>.azurestaticapps.net
```

**Sharing it with a teammate.** install.sh also prints a magic link
that pre-fills the source form so the tester only needs to paste their
own API key:

```
https://<viewer>/sources?url=https%3A%2F%2Frtd-prod-app...&name=prod
```

Each tester mints their own scoped key:

```bash
# CLI scope: full GUI control (create/scope/run/approve). For most testers.
script -qc "az containerapp exec -n rtd-prod-app -g rtd-prod --container backend \
  --command 'python -m app.scripts.mint_api_key --name nasir-browser --scope cli'" /dev/null

# Viewer scope: read-only — buttons hide in the UI.
script -qc "az containerapp exec -n rtd-prod-app -g rtd-prod --container backend \
  --command 'python -m app.scripts.mint_api_key --name auditor-laptop --scope viewer'" /dev/null
```

Tester opens the magic link, signs into Entra in YOUR tenant, pastes
their key → engagement list renders, scope-aware buttons reflect their
key's permissions.

**Security model:** the **backend API key** is the only auth layer. The
viewer's static shell has nothing sensitive in it — no API keys, no
tenant data, no secrets baked into the JS bundle. Loading the page only
gives you a "Add a source" form. You can't read findings, scope, or
events without pasting a valid API key.

This is the same model most modern SPAs use (auth at the API layer, not
at the static-content layer). Trade-off: anyone with the URL can load
the empty viewer shell. They can't *do* anything without a key minted by
you.

**Want page-load gating via Entra ID?** SWA's custom-auth block requires
the **Standard SKU (~$9/mo per deployment)**, not Free. To upgrade:

1. In `infra/azure-kit/modules/viewer.bicep`, set `sku.name = 'Standard'`
   and `sku.tier = 'Standard'`.
2. Restore an `auth` block to `frontend/public/staticwebapp.config.json`
   (see git history pre-`v0.2.0` for the previous shape).
3. Run `az ad app create` + `az staticwebapp appsettings set` to wire
   the AAD app registration (see git history of `install.sh` for the
   automation we had before).

That gives you tenant-scoped Entra sign-in *on top of* the API key.

**Local development.** For hacking on the viewer itself:

```bash
cd RedTeamDashboard
docker compose -f infra/docker-compose.yml up -d frontend
# Browser → http://localhost:3001
```

`http://localhost:3001` is in the default `extraCorsAllowOrigins` so the
deployed backend accepts requests from it.

## Operations / troubleshooting

```bash
# Backend logs (last 60 lines from the active replica)
script -qc "az containerapp logs show -n rtd-prod-app -g rtd-prod \
    --container backend --tail 60 --format text" /dev/null

# Worker logs — same pattern with --container worker
# Redis logs — same with --container redis

# Force a fresh revision after rotating a KV secret
REV=$(az containerapp revision list -n rtd-prod-app -g rtd-prod \
    --query '[?properties.active].name | [0]' -o tsv)
az containerapp revision restart -n rtd-prod-app -g rtd-prod --revision "$REV"

# Re-deploy the same kit (idempotent — Bicep no-ops anything unchanged)
./infra/azure-kit/scripts/install.sh --env prod --location centralus --yes \
    --image-tag v0.2.0     # roll to a new image

# Tear it all down — single command, no leftovers
az group delete -n rtd-prod -y
```

## Costs you should expect

| Resource | ~Monthly |
|---|---|
| Postgres Flexible Server B1ms | $13 |
| Container App (0.75 vCPU / 2 GiB total, 1 replica) | $10–15 |
| Key Vault (Standard) | <$1 |
| Log Analytics (low ingest) | $1–5 |
| **Total per deployment** | **~$25–35/mo** |

Postgres is the floor. If you blow away the RG between engagements you save
it, but you also lose history.

## Things to know before you do this in earnest

- **Single replica is non-negotiable** with this architecture. If you
  outgrow it, the answer is Azure Managed Redis as a separate resource
  (~$50/mo) and unfreezing `minReplicas`.
- **Persistence is off** for the in-pod Redis
  (`--save '' --appendonly no`). On revision restarts the job queue + run
  checkpoints reset. Acceptable for one operator; not for shared multi-day
  runs.
- **CI does not deploy.** Releases tag GHCR images + the kit tarball; you
  run the kit yourself. There is no central control plane that pushes to
  your tenant.
- **GHCR is public** so you don't need any registry credentials in the
  kit. If you fork and republish under your own user, override
  `--image-repo-owner <yourname>`.
- **Default region in `main.bicep` is `eastus2`** but PAYG/personal subs
  reject Postgres there. Use `--location centralus` until you're on an
  EA/CSP sub.
