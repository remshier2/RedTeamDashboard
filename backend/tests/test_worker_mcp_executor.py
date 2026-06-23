"""Worker → MCP execution (Stage 1.5).

When the run envelope has ``mcp_url`` + ``lease_token`` and a worker MCP
API key is configured, the graph factory wires an ``mcp_executor`` that
routes tool calls through the MCP server. Without those, runs fall back
to the local registry. These tests cover three layers:

1. ``_coerce_tool_response`` — the response normalizer (pure, no I/O).
2. ``make_mcp_executor`` — the sync wrapper around ``MultiServerMCPClient``,
   patched to inject scripted MCP responses without an HTTP round-trip.
3. ``build_graph(mcp_executor=...)`` — the dispatch node calls the executor
   instead of the local IMPLEMENTATIONS registry when one is wired.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.models import RiskLevel, ScopeKind
from app.orchestrator.graph import build_graph
from app.orchestrator.scope import ScopeSnapshot
from app.orchestrator.tools import ToolSpec, all_tools
from app.orchestrator.tools.runtime import ToolResult
from app.worker.mcp_executor import _coerce_tool_response, make_mcp_executor

# ---------------------------------------------------------------------------
# _coerce_tool_response — pure normalizer
# ---------------------------------------------------------------------------


def test_coerce_treats_error_dict_as_failed_toolresult() -> None:
    r = _coerce_tool_response({"error": "scope gate denied: foo"})
    assert r.ok is False
    assert r.error == "scope gate denied: foo"


def test_coerce_lease_findings_round_trip_for_worker_persistence() -> None:
    """Leased response surfaces raw findings to the worker (no server-side
    store). The data payload is preserved as the tool message so the LLM
    still sees the result."""
    r = _coerce_tool_response(
        {
            "subdomains": ["a.acme.test", "b.acme.test"],
            "_lease_findings": [
                {"target": "a.acme.test", "severity": "info", "title": "sub"},
                {"target": "b.acme.test", "severity": "info", "title": "sub"},
            ],
        }
    )
    assert r.ok is True
    assert r.findings is not None and len(r.findings) == 2
    assert r.findings[0]["target"] == "a.acme.test"
    assert "_lease_findings" not in r.data
    assert r.data["subdomains"] == ["a.acme.test", "b.acme.test"]


def test_coerce_plain_data_dict_becomes_ok_tool_result() -> None:
    r = _coerce_tool_response({"status": 200, "title": "ok"})
    assert r.ok is True
    assert r.data == {"status": 200, "title": "ok"}
    assert r.findings is None


def test_coerce_tolerates_json_string_response() -> None:
    r = _coerce_tool_response('{"records": [{"a": "1.2.3.4"}]}')
    assert r.ok is True
    assert r.data == {"records": [{"a": "1.2.3.4"}]}


def test_coerce_non_json_string_wraps_under_raw() -> None:
    r = _coerce_tool_response("free-form output")
    assert r.ok is True
    assert r.data == {"raw": "free-form output"}


def test_coerce_scalar_response_wraps_under_value() -> None:
    r = _coerce_tool_response(42)
    assert r.ok is True
    assert r.data == {"value": 42}


# ---------------------------------------------------------------------------
# make_mcp_executor — sync wrapper round-trip with a patched MCP client
# ---------------------------------------------------------------------------


class _FakeMCPTool:
    """Tiny stand-in for a langchain BaseTool that records invocations."""

    def __init__(self, name: str, response: Any) -> None:
        self.name = name
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, args: dict[str, Any]) -> Any:
        self.calls.append(dict(args))
        return self.response


class _FakeMCPClient:
    """Stand-in for ``MultiServerMCPClient`` exposing a fixed tool list."""

    def __init__(self, tools: list[_FakeMCPTool]) -> None:
        self._tools = tools
        self.get_tools = AsyncMock(return_value=tools)


def test_executor_round_trips_call_and_wraps_findings() -> None:
    fake_tool = _FakeMCPTool(
        "subfinder",
        response={
            "subdomains": ["a.acme.test"],
            "_lease_findings": [
                {"target": "a.acme.test", "severity": "info", "title": "sub"},
            ],
        },
    )
    fake_client = _FakeMCPClient([fake_tool])

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        return_value=fake_client,
    ):
        executor = make_mcp_executor(
            "http://backend:8000/mcp",
            lease_token="lease-abc",
            api_key="worker-key",
        )
        result = executor("subfinder", {"domain": "acme.test"})

    assert result.ok is True
    assert result.findings is not None and len(result.findings) == 1
    assert result.findings[0]["target"] == "a.acme.test"
    assert fake_tool.calls == [{"domain": "acme.test"}]


def test_executor_caches_tool_list_across_calls() -> None:
    fake_tool = _FakeMCPTool("dns_lookup", response={"records": ["1.2.3.4"]})
    fake_client = _FakeMCPClient([fake_tool])

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        return_value=fake_client,
    ):
        executor = make_mcp_executor(
            "http://backend:8000/mcp",
            lease_token="lease-cache",
            api_key="worker-key",
        )
        executor("dns_lookup", {"domain": "acme.test"})
        executor("dns_lookup", {"domain": "foo.test"})

    # get_tools should run exactly once even across two tool calls.
    assert fake_client.get_tools.await_count == 1


def test_executor_returns_failed_result_for_unknown_tool() -> None:
    fake_client = _FakeMCPClient([])  # MCP exposes no tools

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        return_value=fake_client,
    ):
        executor = make_mcp_executor(
            "http://backend:8000/mcp",
            lease_token="lease-empty",
            api_key="worker-key",
        )
        result = executor("portscan", {"target": "10.0.0.1"})

    assert result.ok is False
    assert "portscan" in (result.error or "")


def test_executor_surfaces_transport_errors_as_failed_result() -> None:
    class _BoomTool:
        name = "subfinder"

        async def ainvoke(self, _args: dict[str, Any]) -> Any:
            raise RuntimeError("connection refused")

    fake_client = _FakeMCPClient([_BoomTool()])  # type: ignore[list-item]

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        return_value=fake_client,
    ):
        executor = make_mcp_executor(
            "http://backend:8000/mcp",
            lease_token="lease-boom",
            api_key="worker-key",
        )
        result = executor("subfinder", {"domain": "acme.test"})

    assert result.ok is False
    assert "mcp transport error" in (result.error or "")


def test_executor_passes_headers_to_client_constructor() -> None:
    """X-API-Key + X-Lease-Token both flow through to the MCP client."""
    captured_config: dict[str, Any] = {}

    def _capture(config: dict[str, Any]) -> _FakeMCPClient:
        captured_config.update(config)
        return _FakeMCPClient([])

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        side_effect=_capture,
    ):
        make_mcp_executor(
            "http://backend:8000/mcp",
            lease_token="lease-hdr",
            api_key="hdr-api-key",
        )

    assert "rtd" in captured_config
    server_cfg = captured_config["rtd"]
    assert server_cfg["url"] == "http://backend:8000/mcp"
    assert server_cfg["transport"] == "sse"
    assert server_cfg["headers"] == {
        "X-API-Key": "hdr-api-key",
        "X-Lease-Token": "lease-hdr",
    }


# ---------------------------------------------------------------------------
# build_graph(mcp_executor=...) — dispatch node routes to the executor
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """Returns a queued AIMessage per invoke — enough to drive one dispatch
    cycle then end."""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)

    def bind_tools(self, _schemas: list[Any]) -> _ScriptedLLM:
        return self

    def invoke(self, _messages: list[Any]) -> AIMessage:
        if not self._responses:
            return AIMessage(content="done")
        return self._responses.pop(0)


def _scope_for(domain: str) -> list[ScopeSnapshot]:
    return [
        ScopeSnapshot(
            id=uuid.uuid4(),
            kind=ScopeKind.domain,
            value=domain,
            is_exclusion=False,
        )
    ]


def test_graph_routes_dispatch_to_mcp_executor_when_set() -> None:
    """When ``mcp_executor`` is wired, the dispatch node calls it instead of
    ``run_tool`` — proves we don't double-execute by also hitting the local
    IMPLEMENTATIONS registry."""
    captured: list[tuple[str, dict[str, Any]]] = []

    def fake_executor(name: str, args: Mapping[str, Any]) -> ToolResult:
        captured.append((name, dict(args)))
        return ToolResult(ok=True, data={"subdomains": ["x.acme.test"]})

    llm = _ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "subfinder",
                        "args": {"domain": "acme.test"},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    # Filtered registry mirrors what graph_factory passes in for a leased run.
    registry = {
        spec.name: spec
        for spec in all_tools()
        if spec.name == "subfinder"
    }
    graph = build_graph(
        llm=llm,
        registry=registry,
        mcp_executor=fake_executor,
    )

    state = {
        "messages": [
            SystemMessage(content="sys"),
            HumanMessage(content="enumerate acme.test"),
        ],
        "scope_items": _scope_for("acme.test"),
    }
    config = {"configurable": {"thread_id": "t-mcp-route"}}
    list(graph.stream(state, config=config))

    assert captured == [("subfinder", {"domain": "acme.test"})]


def test_graph_falls_back_to_local_when_no_executor() -> None:
    """Without an executor, the dispatch node uses the legacy local path —
    we verify by stubbing the local implementation registry and confirming
    the stub fires."""
    local_calls: list[tuple[str, dict[str, Any]]] = []

    def stub_impl(args: Mapping[str, Any]) -> ToolResult:
        local_calls.append(("subfinder", dict(args)))
        return ToolResult(ok=True, data={"subdomains": []})

    llm = _ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "subfinder",
                        "args": {"domain": "acme.test"},
                        "id": "call_2",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    registry = {
        spec.name: spec for spec in all_tools() if spec.name == "subfinder"
    }
    graph = build_graph(
        llm=llm,
        registry=registry,
        implementations={"subfinder": stub_impl},
    )

    state = {
        "messages": [
            SystemMessage(content="sys"),
            HumanMessage(content="enumerate"),
        ],
        "scope_items": _scope_for("acme.test"),
    }
    config = {"configurable": {"thread_id": "t-local-route"}}
    list(graph.stream(state, config=config))

    assert local_calls == [("subfinder", {"domain": "acme.test"})]


def test_graph_executor_path_still_enforces_scope_gate() -> None:
    """The MCP executor doesn't bypass the local scope gate — out-of-scope
    calls are denied before the executor is reached. Defense in depth."""
    captured: list[tuple[str, dict[str, Any]]] = []

    def fake_executor(name: str, args: Mapping[str, Any]) -> ToolResult:
        captured.append((name, dict(args)))
        return ToolResult(ok=True, data={})

    llm = _ScriptedLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "subfinder",
                        "args": {"domain": "out-of-scope.test"},
                        "id": "call_3",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    registry = {
        spec.name: spec for spec in all_tools() if spec.name == "subfinder"
    }
    graph = build_graph(
        llm=llm,
        registry=registry,
        mcp_executor=fake_executor,
    )

    state = {
        "messages": [
            SystemMessage(content="sys"),
            HumanMessage(content="enumerate"),
        ],
        "scope_items": _scope_for("acme.test"),  # NOT out-of-scope.test
    }
    config = {"configurable": {"thread_id": "t-gate-deny"}}
    list(graph.stream(state, config=config))

    # Executor was never called because the gate denied the call upstream.
    assert captured == []


# ---------------------------------------------------------------------------
# Smoke check — make_mcp_executor returns a callable with ToolResult contract
# ---------------------------------------------------------------------------


def test_executor_is_callable_with_run_tool_contract() -> None:
    """The whole reason for the sync wrapper: dispatch code calls it like
    ``run_tool(name, args)`` — typed (str, Mapping) → ToolResult."""
    fake_client = _FakeMCPClient([_FakeMCPTool("dns_lookup", {"records": []})])
    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        return_value=fake_client,
    ):
        executor = make_mcp_executor(
            "http://x/mcp", lease_token="lease-z", api_key="k"
        )
        out = executor("dns_lookup", {"domain": "acme.test"})
    assert isinstance(out, ToolResult)


# Sanity: prove a tool spec we rely on actually exists with the expected risk
# so the build_graph tests above aren't passing by accident.
def test_subfinder_spec_exists_and_is_passive() -> None:
    spec: ToolSpec | None = next(
        (s for s in all_tools() if s.name == "subfinder"), None
    )
    assert spec is not None
    assert spec.risk is RiskLevel.passive
