import asyncio
import json

import pytest

from opentabular.mcp_server import (
    build_server, describe_model_tool, explain_measure_tool, query_tool, search_model_tool,
)

pytest.importorskip("mcp")


def test_catalogue_hides_hidden_columns_and_dax(sales):
    info = describe_model_tool(sales)
    sales_cols = next(t for t in info["tables"] if t["name"] == "Sales")["columns"]
    assert "Product Key" not in {c["name"] for c in sales_cols}
    assert all("dax" not in m for m in info["measures"])
    info = explain_measure_tool(sales, "[Gross Margin]")
    assert info["dax"] == "[Total Sales] - [Total Cost]"
    assert info["lineage"]["direct_measures"] == ["Total Sales", "Total Cost"]
    assert "Gross Margin %" in info["lineage"]["used_by"]
    assert explain_measure_tool(sales, "Total Sales")["trust"] == "certified"
    assert search_model_tool(sales, "margin")["results"][0]["name"].startswith("Gross Margin")


def test_query_tool_is_governed(sales):
    ok = query_tool(sales, ["Total Quantity"], ["'Product'[Category]"],
                    [{"column": "'Date'[Year]", "op": "=", "value": 2024}], ["Total Quantity desc"], 2)
    assert ok["columns"] == ["Category", "Total Quantity"]
    assert len(ok["rows"]) == 2 and ok["truncated"] is True
    assert ok["rows"][0][1] >= ok["rows"][1][1]
    assert "GROUP BY" in ok["sql"]
    assert ok["measures"] == [{"name": "Total Quantity", "trust": "governed", "owner": None, "format": "#,##0"}]

    # An agent cannot define its own metric or reference unknown objects.
    assert "ad-hoc measures are disabled" in query_tool(sales, ["Mine = SUM(Sales[Quantity])"])["error"]
    assert "unknown measure" in query_tool(sales, ["Revenue"])["error"]
    assert "Unknown column" in query_tool(sales, ["Total Sales"], ["Product[Brand]"])["error"]


def test_server_exposes_the_tools_over_mcp(sales):
    server = build_server(sales)

    async def go():
        tools = await server.list_tools()
        result = await server.call_tool("query", {"measures": ["Orders"]})
        return tools, result

    tools, result = asyncio.run(go())
    assert {t.name for t in tools} == {"describe_model", "search_model", "explain_measure", "query"}
    text = json.dumps(result, default=lambda o: getattr(o, "__dict__", str(o)))
    assert "Orders" in text and "420" in text
