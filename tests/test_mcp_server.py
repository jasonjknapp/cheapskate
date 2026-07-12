# SPDX-License-Identifier: Apache-2.0
"""MCP server: builds, exposes the two tools with correct schemas, and routes
through the existing modules. No stdio transport is started — we inspect the
FastMCP server object and call tools in-process."""

from __future__ import annotations

import asyncio

import pytest

# The MCP server needs the optional ``mcp`` extra. A bare ``pip install -e .[dev]``
# omits it, so skip this whole module cleanly there; CI installs the extra so
# these tests still run in the pipeline.
pytest.importorskip("mcp", reason="needs the 'mcp' extra (pip install 'cheapskate[mcp]')")

from cheapskate import mcp_server  # noqa: E402
from cheapskate.config import Config  # noqa: E402


def test_build_server_registers_two_tools():
    srv = mcp_server.build_server(Config())
    tools = asyncio.run(srv.list_tools())
    names = {t.name for t in tools}
    assert names == {"run_task", "econ_report"}


def test_run_task_tool_schema():
    srv = mcp_server.build_server(Config())
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    schema = tools["run_task"].inputSchema
    props = schema.get("properties", {})
    assert set(props) == {"task_type", "criteria", "payload"}
    assert set(schema.get("required", [])) == {"task_type", "criteria", "payload"}


def test_econ_report_tool_schema_month_optional():
    srv = mcp_server.build_server(Config())
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    schema = tools["econ_report"].inputSchema
    assert "month" in schema.get("properties", {})
    assert "month" not in schema.get("required", [])  # optional


def test_econ_report_tool_runs_with_empty_telemetry():
    srv = mcp_server.build_server(Config())
    _blocks, structured = asyncio.run(srv.call_tool("econ_report", {}))
    assert "No telemetry" in structured["result"]


def test_run_task_tool_never_local_refusal_is_structured():
    # never_local task refuses cleanly through the tool (no exception surfaces to
    # the MCP client — a structured refusal is returned instead).
    cfg = Config(never_local=["financial"])
    srv = mcp_server.build_server(cfg)
    _blocks, structured = asyncio.run(
        srv.call_tool("run_task", {"task_type": "financial", "criteria": "c", "payload": "d"})
    )
    # FastMCP wraps a dict return under "result"
    result = structured.get("result", structured)
    assert result["route"] == "refused"
    assert result["class"] == "never_local"


def test_d6_run_task_tool_local_unavailable_refusal_is_structured(monkeypatch):
    # D6: when task.run raises LocalUnavailable (e.g. a role with no model, or a
    # never-cloud task that cannot run locally), the MCP tool must return a
    # structured refusal, not let the exception surface to the client. The tool
    # closes over `task` imported inside build_server, so patch the router module.
    from cheapskate.router import task as task_mod

    def raise_local_unavailable(*a, **k):
        raise task_mod.LocalUnavailable("no model configured for role")

    monkeypatch.setattr(task_mod, "run", raise_local_unavailable)
    srv = mcp_server.build_server(Config())
    _blocks, structured = asyncio.run(
        srv.call_tool("run_task", {"task_type": "summarize", "criteria": "c", "payload": "d"})
    )
    result = structured.get("result", structured)
    assert result["route"] == "refused"
    assert result["class"] == "local_unavailable"


def test_serve_without_mcp_extra_raises_clear_error(monkeypatch):
    # Simulate the 'mcp' package being absent: build_server must raise an
    # ImportError naming the extra.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "mcp.server.fastmcp" or name.startswith("mcp.server"):
            raise ImportError("No module named 'mcp'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError) as e:
        mcp_server.build_server(Config())
    assert "mcp" in str(e.value)
    assert "extra" in str(e.value)
