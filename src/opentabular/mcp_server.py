"""MCP server: governed, self-service access to the semantic model for AI
clients.

The client never writes SQL or DAX. It reads the catalogue, then asks for
measures by name, grouped and filtered by model columns; the compiler turns
that into SQL. Ad-hoc measures are rejected, so every number an agent reports
comes from a definition that lives in the repository.

Run with:  otab mcp --project path/to/project
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from .compiler import CompileError, Query
from .project import Project

MAX_ROWS = 1000

INSTRUCTIONS = (
    "This server exposes a governed semantic model. Call describe_model first to see the "
    "available measures and columns. Answer data questions with the query tool, using measure "
    "names and 'Table'[Column] references exactly as listed. Do not compute metrics yourself "
    "from raw columns: if no measure fits the question, say so. When you report a number, you "
    "can cite its definition with explain_measure."
)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    return v


def describe_model_tool(project: Project) -> dict:
    return project.describe(include_dax=False)


def explain_measure_tool(project: Project, name: str) -> dict:
    info = project.explain(name)
    return info if info is not None else {"error": f"unknown measure {name!r}"}


def search_model_tool(project: Project, text: str) -> dict:
    return {"results": project.search(text)}


def query_tool(
    project: Project,
    measures: list[str],
    group_by: list[str] | None = None,
    filters: list[Any] | None = None,
    order_by: list[str] | None = None,
    limit: int | None = None,
) -> dict:
    limit = MAX_ROWS if limit is None else max(0, min(int(limit), MAX_ROWS))
    q = Query(measures=list(measures or []), group_by=list(group_by or []),
              filters=list(filters or []), order_by=list(order_by or []), limit=limit + 1)
    try:
        result = project.run(q, allow_adhoc_measures=False)
    except CompileError as e:
        return {"error": str(e)}
    rows = result.rows[:limit]
    return {
        "columns": result.columns,
        "rows": [[_jsonable(v) for v in r] for r in rows],
        "truncated": len(result.rows) > limit,
        "measures": [
            {"name": c.name, "trust": c.trust, "owner": c.owner, "format": c.format_string}
            for c in result.meta if c.kind == "measure"
        ],
        "sql": result.sql,
    }


def build_server(project: Project):
    try:  # mcp >= 2
        from mcp.server.mcpserver import MCPServer as _Server
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _Server

    server = _Server(f"opentabular: {project.model.name}", instructions=INSTRUCTIONS)

    @server.tool()
    def describe_model() -> dict:
        """List the tables, columns, measures and relationships of the semantic
        model, with their descriptions. Call this before querying."""
        return describe_model_tool(project)

    @server.tool()
    def search_model(text: str) -> dict:
        """Find measures and columns by words in their name or description.
        Use this when the catalogue is large or the user's wording is loose."""
        return search_model_tool(project, text)

    @server.tool()
    def explain_measure(name: str) -> dict:
        """Explain how a number is calculated: the measure's description, owner,
        trust level, DAX definition, and its lineage (the measures, columns and
        physical tables it is built from, and the measures that depend on it)."""
        return explain_measure_tool(project, name)

    @server.tool()
    def query(
        measures: list[str],
        group_by: list[str] | None = None,
        filters: list[Any] | None = None,
        order_by: list[str] | None = None,
        limit: int | None = None,
    ) -> dict:
        """Evaluate measures from the semantic model.

        measures: measure names exactly as returned by describe_model.
        group_by: columns to break the measures down by, as 'Table'[Column].
        filters:  conditions on one column each, either a string such as
                  "'Product'[Category] = \\"Bikes\\"" or
                  "'Date'[Year] IN {2023, 2024}", or an object
                  {"column": "'Date'[Year]", "op": ">=", "value": 2023}
                  (ops: = != < <= > >= in not_in).
        order_by: output column names, optionally followed by " desc".
        limit:    maximum rows (capped at 1000).

        Returns columns, rows, whether the result was truncated, the trust
        level and owner of each measure, and the SQL that was run.
        """
        return query_tool(project, measures, group_by, filters, order_by, limit)

    return server


def serve(project: Project) -> None:
    project.connect()
    build_server(project).run()
