# SPDX-License-Identifier: Apache-2.0
"""A stdio MCP server exposing Cheapskate's routing + receipts to MCP clients.

Thin over the existing modules — no new behavior, just an adoption surface. Any
MCP-capable tool (a code assistant, an agent CLI) can register ``cheapskate mcp``
and get two tools:

  * ``run_task(task_type, criteria, payload)`` — route + run one supervised
    subtask through :func:`cheapskate.router.task.run` (verify-and-repair,
    fail-closed safety classes). Returns the router result dict.
  * ``econ_report(month?)`` — the monthly savings receipt text from
    :mod:`cheapskate.econ.report`.

The ``mcp`` package is an optional extra: this module imports it lazily inside
:func:`build_server` / :func:`serve` so the base install (and the rest of the
package) stays importable without it, with a clear error naming the extra.
"""

from __future__ import annotations

from typing import Any

_MISSING_MCP = (
    "the MCP server needs the 'mcp' extra; install it with: "
    "pip install 'cheapskate[mcp]'"
)


def build_server(config: Any = None) -> Any:
    """Construct the FastMCP server with the two Cheapskate tools registered.

    ``config`` is loaded once if omitted. Raises a clear ImportError (naming the
    extra) when the ``mcp`` package is not installed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover — exercised via the CLI path
        raise ImportError(_MISSING_MCP) from exc

    from . import config as _config
    from .econ import report as _report
    from .router import routes as _routes
    from .router import task as _task

    cfg = config if config is not None else _config.load()

    server = FastMCP(
        name="cheapskate",
        instructions=(
            "Cheapskate routes each task to the cheapest model that passes your "
            "quality bar — local or cloud — and shows the receipts. Use run_task "
            "to route + run one supervised subtask (with acceptance criteria and "
            "verify-and-repair), and econ_report for the monthly savings receipt."
        ),
    )

    @server.tool(
        name="run_task",
        description=(
            "Route and run one supervised subtask. task_type selects the routing "
            "rule (e.g. summarize, draft, classify, review); criteria are the "
            "acceptance criteria the output must meet; payload is the input. "
            "Returns the router result including route (local|cloud), ok, "
            "retries, escalated, and the output. Fail-closed on never_local / "
            "never_cloud safety classes."
        ),
    )
    def run_task(task_type: str, criteria: str, payload: str) -> dict[str, Any]:
        try:
            return _task.run(task_type, criteria, payload, cfg)
        except _routes.NeverLocal as exc:
            return {"task_type": task_type, "route": "refused",
                    "class": "never_local", "reason": str(exc)}
        except _routes.NeverCloud as exc:
            return {"task_type": task_type, "route": "refused",
                    "class": "never_cloud", "reason": str(exc)}
        except _task.CloudUnavailable as exc:
            return {"task_type": task_type, "route": "refused",
                    "class": "cloud_unavailable", "reason": str(exc)}
        except _task.LocalUnavailable as exc:
            return {"task_type": task_type, "route": "refused",
                    "class": "local_unavailable", "reason": str(exc)}

    @server.tool(
        name="econ_report",
        description=(
            "The monthly savings receipt: per-task-type routing recommendations "
            "and the true local-vs-cloud cost table, computed from measured "
            "telemetry. Optional month is 'YYYY-MM' (defaults to the current "
            "month). Returns plain report text."
        ),
    )
    def econ_report(month: str | None = None) -> str:
        bundle = _report.generate(cfg, month=month)
        if not bundle.reports:
            return f"No telemetry yet for {month or 'this month'} — run some tasks first."
        return _report.render_report(
            bundle.reports, bundle.receipts,
            pricing_origin=bundle.pricing_origin,
            staleness=bundle.staleness,
            power=bundle.power,
        )

    return server


def serve(config: Any = None) -> None:
    """Run the stdio MCP server (blocking). Raises the extra-naming ImportError
    if ``mcp`` is not installed."""
    server = build_server(config)
    server.run(transport="stdio")
