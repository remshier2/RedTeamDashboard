# Red Team Dashboard

Multi-engagement red team operations dashboard with orchestrator agents,
cost tracking, and governance controls. Each phase of an engagement
(OSINT → Scan → Verify → Exploit → PrivEsc → Persistence → Cleanup → Report)
runs as its own LangGraph ReAct agent. Operator approves active/destructive
tool calls in the UI.

## Status

**Active development.** Phases 7–9 complete; Phases 10–11 in progress.

- ✅ **Phase 7**: Single-tenant pivot, Entra SSO, dark monochrome UI
- ✅ **Phase 8**: Findings validation, observations system, findings bulk import
- ✅ **Phase 9**: Strategic + Tactical orchestrator agents, task queue, suggestions
- 🔄 **Phase 10**: Hybrid execution (import-first model), ephemeral executor
- 🔄 **Phase 11**: Cost engine (LLM spend tracking, rollup, Costs tab)

Architecture and phase roadmap live in `docs/ARCHITECTURE_SKETCH_V2.md` and
`docs/CHARTER.md`.

## Stack

- Backend: Python / FastAPI (thin control plane) + LangGraph orchestrator worker
- Frontend: Next.js App Router + React + TS + Tailwind + shadcn/ui
- Data: PostgreSQL (source of truth, LangGraph checkpointer) + Redis
  (Streams jobs, pub/sub events)
- Streaming: SSE
- Hosting: Azure Container Apps (prod), docker-compose (local)
- LLMs: Anthropic Claude (orchestrator + workers), OpenAI (optional)
- Auth: Entra ID OIDC (per-analyst SSO) or API key (CLI)

## Layout

```
backend/    FastAPI app + LangGraph worker + orchestrator agents
frontend/   Next.js App Router
cli/        `rtd` CLI tool
infra/      docker-compose.yml + azure/ Bicep
docs/       architecture, charter, deployment docs
```

## Local dev

```bash
cp infra/.env.example infra/.env
docker compose -f infra/docker-compose.yml up --build
```

- Frontend: http://localhost:3001
- Backend:  http://localhost:8000/health
- Postgres: localhost:5432
- Redis:    localhost:6379
