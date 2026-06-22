"""Phase 9 orchestrator agents.

Two-tier model layered ABOVE the per-run worker:

- ``StrategicAgent`` — the Watcher. Reads findings, proposes next-step tasks.
  Pure observer: never executes tools, never dispatches; just writes
  ``Suggestion`` rows for analyst review.
- ``TacticalAgent`` — the Manager. Takes accepted Tasks, dispatches worker
  runs for agent-eligible scan/enum work. HARD-REFUSES ``exploit`` tasks —
  agents scan, analysts exploit (CHARTER invariant).

Both agents log every LLM call in ``agent_executions`` for the Costs tab.
"""
from __future__ import annotations

from app.agents.strategic import StrategicAgent
from app.agents.tactical import TacticalAgent, TacticalRefusedExploit

__all__ = ["StrategicAgent", "TacticalAgent", "TacticalRefusedExploit"]
