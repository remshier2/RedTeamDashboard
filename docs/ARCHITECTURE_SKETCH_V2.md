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

# Red Team Dashboard — Combined Architecture Sketch (v2)
## Single-tenant operations platform · agent orchestration · cost tracking

**Status:** Active reference document. Phase 7–9 complete; Phase 10–11 in progress.
**Supersedes:** `AGENT_ORCHESTRATION_ARCHITECTURE_SKETCH.md` (v1) + the in-repo
Phase 7+ plan. This document merges them into one target.

---

## 0. What changed in the merge (read this first)

The two prior sketches were ~80% complementary. This v2 keeps the strongest
ideas from each and resolves the few conflicts:

| From the **orchestration sketch (v1)** we KEEP | From the **dashboard plan** we KEEP |
|---|---|
| Validation stage (results → analyst sanity-check → findings) | Single-tenant hosted deployment, Entra per-analyst sign-in |
| Estimate-vs-actual cost, variance, learning, rollup, billing | Tabbed engagement workspace (6 phase tabs) |
| Workflow templates (named, parameterized, multi-task) | Strategic (watcher) + Tactical (manager) + Workers tiers |
| Explicit task queue with per-task lifecycle + cost | All-black monochrome UI + one accent |
| Heavy-tool ambition (Nessus / Maltego / Dehashed) as a goal | Approval gate before active tools; scope auto-reject |

**Conflicts resolved:**
- **Execution substrate:** keep the existing in-pod worker for light tasks;
  add ephemeral containers/VMs as a *pluggable backend* for heavy/isolated
  work — not a wholesale rebuild. Both sit behind one Task interface.
- **Cost actuals:** do NOT query Azure Cost Management at task completion (its
  data lags hours–days). Compute cost at completion from `duration × SKU rate`,
  reconcile against Cost Management asynchronously.
- **No duplicate schema / no third service:** extend the existing SQLAlchemy
  models + FastAPI/worker rather than standing up a parallel `findings` table
  or a separate Node "Agent Orchestrator."

---

## 1. North-star goals

1. **One place to view everything** — a single hosted dashboard in the firm's
   own Azure tenant; analysts sign in with their org identity.
2. **Engagement = tabbed workspace** per target: OSINT Recon · Vuln Scan ·
   Exploit · Phishing · Results · Costs, with a Strategic command-center view.
3. **Actionable findings** — every finding is a launch point: click it to see
   the tasks needed to exploit it.
4. **Analyst chooses who scans** — agent-run or analyst-run, per scan.
5. **Agents scan; analysts exploit.** Hard invariant (see §7).
6. **Strategic stays organized** — maintains what happened / is happening /
   needs to happen, prices suggestions, and decides when an ephemeral resource
   is warranted.
7. **Cost-aware end to end** — every task carries an estimate and an actual;
   engagements roll up to an **internal effort/cost** number (not client
   billing).

---

## 2. System overview

```
                         ┌───────────────────────────────────────┐
   Analyst (Entra SSO) ──►│  DASHBOARD (Next.js SWA, all-black)     │
                          │  engagement list · 6 tabs · validation  │
                          │  queue · cost view · suggestions feed    │
                          └───────────────┬─────────────────────────┘
                                          │ Bearer (MSAL) + API (CLI)
                          ┌───────────────▼─────────────────────────┐
                          │  BACKEND CONTROL PLANE (FastAPI)         │
                          │  engagements · findings · tasks · scope  │
                          │  approvals · validation · cost engine    │
                          └──────┬───────────────────────┬──────────┘
                                 │ Redis Streams (jobs)   │ Claude API
            ┌────────────────────▼─────────┐   ┌──────────▼───────────────┐
            │  TACTICAL (manager + queue)   │   │  STRATEGIC ("the Watcher")│
            │  decompose → queue → dispatch │◄──┤  observes everything      │
            │  SCANNING ONLY · gated        │   │  plan + priced suggestions│
            └───┬───────────────────┬───────┘   │  decides ephemeral need   │
                │ light             │ heavy      │  PURE WATCHER (never acts) │
        ┌───────▼──────┐    ┌───────▼─────────┐  └────────────▲──────────────┘
        │ in-pod WORKER │    │ EPHEMERAL exec  │               │ results
        │ (LangGraph)   │    │ (ACI / VM,       │               │
        │ passive+enum  │    │  approval-gated) │───────────────┘
        └───────┬───────┘    └───────┬──────────┘
                └──────── results ────┴──► PENDING_VALIDATION
                                              │ analyst APPROVE
                                              ▼
                                          FINDINGS (validated, actionable)
                                              │
                          ┌───────────────────┼───────────────────┐
                          ▼                    ▼                   ▼
                   exploit-task list      Results tab → PDF     Cost engine
                   (analyst-only)                               → Costs tab
```

---

## 3. The three tiers + the analyst

### STRATEGIC — "the Watcher" (Claude, pure observer)
- Watches all findings, tasks, runs, events; maintains the **living plan**:
  what HAPPENED · what's HAPPENING · what NEEDS to happen.
- Produces **priced suggestions** — "found web server → web-app scan (~$X)";
  this is v1's "Engagement Orchestrator," available both continuously and via
  an on-demand **"Analyze & suggest next"** action.
- Generates the **exploit-task list** behind each actionable finding (§6).
- **Decides whether a finding warrants an ephemeral resource** (§8); if not,
  defers it.
- **Never executes.** It recommends; the analyst and Tactical act. (Its
  ephemeral-spin-up "decision" is a *recommendation that opens an
  approval-gated request* — Strategic does not provision directly.)

### TACTICAL — "the Manager" (AI + task queue)
- Decomposes an objective/workflow into concrete tasks, queues, sequences,
  and dispatches them to workers.
- **Auto-dispatch, gated:** passive tasks run autonomously; **active
  enumeration** still stops at the approval gate before running.
- **SCANNING ONLY** — Passive OSINT + Active Enumeration. **Tactical can never
  run exploitation tasks** (§7).
- Routes light tasks to the in-pod worker, heavy/isolated tasks to ephemeral
  execution — same Task interface.

### WORKERS — execution (existing model + pluggable heavy backend)
- **In-pod worker** (today): LangGraph + per-run LLM, pure-Python passive +
  active-enum tools, pulls Redis Streams. Covers most scanning.
- **Ephemeral executor** (new, pluggable): an Azure Container Instance or VM
  spun up for a heavy/isolated tool, runs it, pre-parses, callbacks, dies.
  Used only when Strategic flags the need and the analyst approves.
- All worker output lands as `PENDING_VALIDATION` — never directly as a
  finalized finding.

### ANALYST (human)
- Signs in via Entra; owns scope, approvals, validation.
- **Performs all exploitation**, manually, and **uploads** the results/artifacts.
- Chooses, per scan, whether Tactical or they themselves run it (§5).

---

## 4. Scope & the two human gates

Two *different* human controls, both kept:

1. **Scope gate** (automatic, pure function) — out-of-scope targets are
   auto-rejected, never prompt a human.
2. **Approval gate** (before execution) — every active/enumeration tool call
   triggers an interrupt; analyst approves / denies / edits args. Session
   grants (`authorizations`) let long runs avoid re-prompting per call.
3. **Validation gate** (after execution) — raw results land as
   `pending_validation`; the analyst sanity-checks ("looks right / wrong") and
   approves → **findings are created** (status `validated`). Rejected results
   can be re-queued. *This is new, from v1.*

> Approval answers "may I run this?" · Validation answers "are these results
> real enough to be a finding?"

---

## 5. Analyst-choice scanning

For each Passive OSINT or Active Enumeration scan, the analyst picks the
**owner** at queue time:

```
Scan task created ──► owner = ?
   ├─ AGENT     → Tactical dispatches a worker → results → Strategic
   └─ ANALYST   → analyst runs it in their own kit → uploads output
                  → parsed into results → Strategic
```

Either path produces the same structured results and feeds the **Strategic**
orchestrator identically. This is the "Hybrid" model made explicit: the app
can *run* scans or *ingest* them, analyst's call, scan by scan.

---

## 6. Actionable findings

A finding is not a dead-end record — it is the entry point to the next move.

```
FINDING (validated)
  ├─ summary, severity, target, phase, source
  └─ ▼ click ─────────────────────────────────────────────┐
       EXPLOIT-TASK LIST (generated by Strategic)          │
       • Task: "Test SQLi on /login"   [analyst-only]      │
       • Task: "Enumerate DB users"    [agent-eligible]    │
       • Task: "Spin Kali box to exploit"  → ephemeral req │
       each task: type · owner-eligibility · cost estimate │
       ───────────────────────────────────────────────────┘
```

- **Exploitation tasks are analyst-only** (agents never exploit). They become a
  checklist the analyst works through and uploads results for.
- **Further-enumeration tasks** suggested off a finding *may* be delegated to
  Tactical (analyst's choice, §5).
- Tasks link back to the finding (`task.finding_id`) so the plan, the finding,
  and the eventual report all stay connected.

---

## 7. Hard invariant: agents scan, analysts exploit

```
            PASSIVE OSINT   ACTIVE ENUM   EXPLOITATION
Tactical /
worker agent      ✅              ✅ (gated)        ❌  NEVER
Analyst           ✅              ✅                ✅  (then uploads)
```

- No agent — Tactical, worker, or ephemeral — may run an exploitation task.
  Enforced in the tool registry (tools tagged `exploit` are not dispatchable by
  any agent path) **and** at dispatch (Tactical refuses `exploit`-class tasks).
- Exploitation results enter the system only via analyst **upload**, then go
  through the validation gate like any other result.

---

## 8. Ephemeral resource model (Strategic-decided, gated, deferrable)

```
Strategic evaluates a finding
   │
   ├─ "needs ephemeral resource?"  ──NO──► DEFER (recorded; revisitable)
   │
   └─ YES ─► creates an EPHEMERAL REQUEST (kind: scan-box | attack-box)
              │  carries: reason, finding_id, est. cost, est. lifetime
              ▼
        ANALYST APPROVES  (approval gate — provisioning costs money)
              │
              ▼
        Execution layer provisions ACI/VM → tool runs (scan) OR
        analyst uses the box (exploit) → results/artifacts → validation
              │
              ▼
        Resource torn down; actual cost finalized from duration × SKU rate
```

- Strategic **decides need** and **defers** when not warranted — it does not
  provision. Provisioning is approval-gated and executed by the execution
  layer (consistent with "pure watcher").
- An **attack-box** ephemeral is a **Kali VM the analyst RDP/SSHes into** to
  exploit from — the agent still never exploits; it just provisioned the box on
  request. (scan_box stays a container for heavy scan tools.)
- Deferred requests are kept so Strategic can re-raise them as the engagement
  evolves.

---

## 9. Engagement lifecycle (the loop)

```
1. CREATE engagement (target, scope, objectives) — analyst, Entra-identified
2. PLAN          Strategic frames phases/objectives
3. SCAN          analyst picks workflow + owner (agent | self) per scan
                 → passive auto-runs; active enum hits approval gate
4. RESULTS       land PENDING_VALIDATION (whether agent- or analyst-sourced)
5. VALIDATE      analyst APPROVE → findings (validated)
6. SUGGEST       Strategic analyzes findings → priced next-task suggestions
                 + per-finding exploit-task lists; flags ephemeral needs
7. SELECT        analyst cherry-picks: delegate enum to Tactical, or take
                 exploit tasks themselves, or approve an ephemeral box
8. EXPLOIT       analyst-only; results uploaded → back to step 4
9. [loop 3–8 across phases]
10. REPORT       Results tab aggregates validated findings → PDF
11. COSTS        rollup of infra + LLM + labor → billable total
```

---

## 10. Data model (extend existing; add new)

**Reuse / extend (already in the codebase):**
- `engagements`, `users` (has `entra_oid`), `scope_items`, `approvals`,
  `authorizations` (session grants), `audit_log`, `api_keys`.
- **`findings`** — extend with:
  `phase` (osint | vuln_scan | exploit | phishing | general),
  `status` (pending_validation | validated | rejected | false_positive),
  `validated_by`, `validated_at`, `task_id` (source task),
  `finding_type` (vulnerability | osint | credential | …).
  *(Keeps existing `severity`, `summary`, `details` JSONB, `source_tool`,
  `target`.)*

**New tables:**
```
workflow_templates(id, name, description, phase, tasks JSONB[, params schema])
tasks(id, engagement_id, finding_id?, workflow_id?, phase,
      task_type, task_class[scan|enum|exploit], owner[agent|analyst],
      parameters JSONB, status[suggested|queued|running|completed|failed|deferred],
      estimated_cost, actual_cost, created_at, started_at, completed_at)
task_results(id, task_id, raw_results JSONB, parsed_results JSONB,
             status[pending_validation|validated|rejected],
             validated_by, validated_at, rejection_reason)
suggestions(id, engagement_id, finding_id?, text, reasoning,
            estimated_cost, status[open|accepted|dismissed], source=strategic)
agent_executions(id, task_id, engagement_id, backend[in_pod|aci|vm],
                 azure_resource_id?, start, end, duration_min,
                 estimated_cost, actual_cost, status, error)
ephemeral_requests(id, engagement_id, finding_id, kind[scan_box|attack_box],
                   reason, status[proposed|approved|denied|active|torn_down|deferred],
                   est_cost, est_lifetime, approved_by)
cost_rollup(id, engagement_id, infra_actual, llm_actual, labor_actual,
            total_estimated, breakdown JSONB)
labor_entries(id, engagement_id, phase, user_id, hours, rate)
cost_variance(id, execution_id, task_type, estimated, actual, variance_pct)
```

---

## 11. Cost engine (combined)

Three dimensions, one rollup:

- **Infra** (from v1): per-task `estimated_cost` at queue time
  (`duration × current SKU rate` from a dynamic pricing lookup), `actual_cost`
  at completion (`measured duration × SKU rate`), variance tracked, estimates
  **learn** from history. Reconcile against Azure Cost Management asynchronously
  (not at completion — it lags).
- **LLM** (from dashboard plan): token spend per Strategic/Tactical/worker run,
  attributed to the engagement.
- **Labor**: **manual time logging** — analyst enters hours (× rate) per phase.
- **Rollup**: `cost_rollup` sums all three per engagement → Costs tab + PDF.
  **Internal effort tracking only — not client billing.** Actuals are for our
  own visibility and estimate-learning, so they don't need billing-grade
  precision (a key simplification: no hard dependency on Azure Cost Mgmt
  reconciliation).

---

## 12. UI / dashboard

- **Auth:** MSAL.js Entra sign-in (per-analyst identity); works on SWA **Free**
  SKU. Backend validates the Bearer token → `User.entra_oid`. CLI keeps its
  API-key path.
- **Home:** engagement list, analyst identity in header.
- **Engagement page — tabs:** OSINT Recon · Vuln Scan · Exploit · Phishing ·
  Results · Costs, plus a **Strategic command-center** (timeline · live status ·
  next-up backlog · priced suggestions feed) and a **Validation queue**.
- **Findings** render as actionable cards (click → exploit-task list, §6).
- **Look:** all-black monochrome minimalism, hairline borders, grayscale text
  ramp, **one accent** (proposed: ember red `#E5484D`, doubles as the critical
  signal). No shadows/gradients.

---

## 13. Execution substrate

- **In-pod worker** stays the default (single Container App, 1 replica, Redis
  Streams, LangGraph) — proven, cheap, covers passive + active-enum tools.
- **Ephemeral executor** is added as a second backend behind the Task
  interface, invoked only on an approved `ephemeral_request`. ACI for
  containerized tools; VM for an analyst attack-box.
- **Import path** (analyst-run scans/exploits) feeds the same `task_results`
  pipeline as agent-run tasks.

---

## 14. Hard rules / safety invariants

1. Out-of-scope → auto-rejected, never executed (scope gate).
2. Active/enumeration tools → approval gate before running.
3. **Agents never exploit** — enforced in the tool registry and at dispatch.
4. Exploitation results enter only via analyst upload.
5. Raw results are never findings until the validation gate passes.
6. Provisioning an ephemeral resource is always approval-gated (it costs money).
7. Strategic never acts — it observes, plans, prices, recommends, defers.
8. Every auto-approval / dispatch / provision is audit-logged with its covering
   authorization.

---

## 15. Key design decisions

| Aspect | Decision | Rationale |
|---|---|---|
| Tenancy | Single shared deployment, firm's tenant | "One place to view everything" |
| Identity | Entra per-analyst via MSAL (Free SKU) | Attribution without paid SKU |
| Tactical autonomy | Auto-dispatch, gated; **scanning only** | Fast but safe; never exploits |
| Strategic role | Pure watcher; decides ephemeral need only | Human keeps control |
| Exploitation | Analyst-only, uploaded | Legal/operational control |
| Scan ownership | Analyst chooses agent vs self, per scan | Flexibility (the "Hybrid" choice) |
| Findings | Actionable; spawn exploit-task lists | Drive the next move |
| Result→finding | Validation gate in between | Quality control |
| Heavy tools | Import-first; ephemeral exec pluggable later | Ship fast, grow into it |
| Attack-box | Kali VM, analyst RDP/SSH | Analyst exploits from it; agent never does |
| Cost purpose | Internal effort tracking, not billing | No billing-grade actuals needed |
| Cost actuals | duration × SKU at completion; no hard Cost-Mgmt dep | Cost Mgmt API lags; internal use only |
| Labor | Manual time logging | Simple, accurate enough for internal effort |
| Strategic cadence | Continuous watch + on-demand "analyze now" | Best of both |
| Workflow templates | Seed starter set now, accrue from use | Useful day one, grows naturally |
| Schema | Extend existing models, no duplicates | One source of truth |

---

## 16. Resolved decisions (2026-06-16)

1. **Ephemeral attack-box → Kali VM the analyst RDP/SSHes into.** Provisioned
   on an approved ephemeral_request; the analyst exploits from it; the agent
   never does. scan_box remains a container for heavy scan tools.
2. **Heavy scan tools → import-first.** Analyst runs Nessus/Maltego/Dehashed in
   their own kit and uploads output → parsed to results. Active ACI execution
   stays a later, pluggable option.
3. **Strategic cadence → both.** Continuous watching/logging/suggesting AND an
   on-demand "Analyze findings & suggest next" action.
4. **Workflow templates → seed a starter set now** (Network Recon, OSINT enum,
   Web App) and build/accrue more from use.
5. **Cost → internal effort tracking only** (not client billing). Simplifies
   the cost engine: no hard dependency on billing-grade Azure Cost Management
   reconciliation; actuals are for visibility + estimate-learning.
6. **Labor → manual time logging** (analyst enters hours per phase).

All §16 items resolved → cleared to build Phase 7.

---

## 17. Build order (status as of 2026-06-18)

| Phase | Description | Status |
|---|---|---|
| **Phase 7** | Identity + single-tenant pivot + dark monochrome shell + dashboard | ✅ Completed |
| **Phase 8** | Tabbed engagement page; extend `findings` (`phase`, `status`, validation fields); **validation queue** | ✅ Completed |
| **Phase 9** | Orchestrator: Strategic watcher + Tactical manager + task queue + workflow templates + analyst-choice scanning + actionable findings | ✅ Completed |
| **Phase 10** | Hybrid ingest (nmap/Nessus/recon import) + ephemeral executor (pluggable) + ephemeral-request flow | 🔄 In Progress |
| **Phase 11** | Cost engine (LLM spend tracking, rollup, Costs tab UI) + Results→PDF polish | 🔄 In Progress |

**Completed highlights:**
- Phase 7: Single-tenant deployment, Entra SSO per-analyst, dark monochrome UI
- Phase 8: Findings validation workflow (pending → validated), observations system, findings bulk import
- Phase 9: Strategic agent (watcher/suggester), Tactical agent (dispatcher), task queue, suggestions, agent executions tracking

**In progress:**
- Phase 10: Hybrid execution path (import-first model), ephemeral executor backend
- Phase 11: Cost rollup API (`GET /engagements/{slug}/costs`), pricing engine (`pricing.py`), Costs tab frontend component

**Next steps:**
- Labor time logging per phase (manual entry)
- Cost variance tracking (estimate vs actual)
- Azure Cost Management reconciliation (async)
- Ephemeral attack-box flow (Kali VM provisioning)

---

**Created:** 2026-06-16 · **Updated:** 2026-06-18 · **For:** joint reference  
**Status:** Phases 7–9 complete; Phases 10–11 in progress; §16 decisions remain valid.
