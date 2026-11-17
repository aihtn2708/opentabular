"""DAX queries (EVALUATE ...), on the hand-checkable tiny data set."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from opentabular import CompileError
from opentabular.dax.query import DaxQueryError, parse_dax_query
from opentabular.security import Identity
from opentabular.server import make_server


def dax(project, text, adhoc=True, identity=None):
    return project.run_dax(text, adhoc, identity).records()


def test_summarizecolumns_with_columns_filters_and_measures(tiny):
    rows = dax(tiny, 'EVALUATE SUMMARIZECOLUMNS(Product[Category], "Revenue", [Total], "Units", SUM(Sales[Qty]))')
    assert rows == [
        {"Product[Category]": "Bikes", "[Revenue]": 1500, "[Units]": 9},
        {"Product[Category]": "Parts", "[Revenue]": 150, "[Units]": 15},
        {"Product[Category]": None, "[Revenue]": 50, "[Units]": 1},
    ]
    # A filter restricts the grouped column; it does not replace the grouping.
    rows = dax(tiny, 'EVALUATE SUMMARIZECOLUMNS(Product[Color], Product[Color] = "Red", "T", [Total])')
    assert rows == [{"Product[Color]": "Red", "[T]": 750}]
    rows = dax(tiny, """EVALUATE SUMMARIZECOLUMNS('Date'[Year], TREATAS({"Online"}, Sales[Channel]),
                        FILTER(VALUES(Product[Color]), Product[Color] <> "Blue"), "T", [Total])""")
    assert rows == [{"Date[Year]": 2023, "[T]": 250}, {"Date[Year]": 2024, "[T]": 300}]


def test_row_values_constructor_define_order_and_topn(tiny):
    assert dax(tiny, 'EVALUATE ROW("A", [Total], "B", [Qty])') == [{"[A]": 1700, "[B]": 25}]
    assert dax(tiny, "EVALUATE { [Total] }") == [{"[Value]": 1700}]
    assert dax(tiny, "EVALUATE VALUES(Product[Color])") == [{"Product[Color]": "Blue"}, {"Product[Color]": "Red"}]
    text = """
        DEFINE
            MEASURE Sales[Double] = [Total] * 2
            MEASURE Sales[Total] = SUM(Sales[Qty])        -- shadows the model measure in this query
        EVALUATE
            SUMMARIZECOLUMNS(Product[Name], "D", [Double], "T", [Total])
        ORDER BY [D] DESC, Product[Name]
    """
    rows = dax(tiny, text)
    assert [r["Product[Name]"] for r in rows] == ["C", "A", "B", None]
    assert rows[0] == {"Product[Name]": "C", "[D]": 30, "[T]": 15}
    top = dax(tiny, 'EVALUATE TOPN(2, SUMMARIZECOLUMNS(Product[Name], "T", [Total]), [T], DESC)')
    assert top == [{"Product[Name]": "B", "[T]": 900}, {"Product[Name]": "A", "[T]": 600}]
    calc = dax(tiny, 'EVALUATE CALCULATETABLE(ROW("T", [Total]), Product[Category] = "Bikes")')
    assert calc == [{"[T]": 1500}]


def test_governed_mode_allows_model_measures_only(tiny):
    result = tiny.run_dax('EVALUATE SUMMARIZECOLUMNS(Product[Category], "Revenue", [Total])', False)
    assert result.meta[1].trust == "governed" and result.rows[0] == ("Bikes", 1500)
    for text, message in [
        ('EVALUATE ROW("X", SUM(Sales[Qty]))', "ad-hoc expression"),
        ('EVALUATE ROW("X", [Total] * 2)', "ad-hoc expression"),
        ('DEFINE MEASURE Sales[X] = 1 EVALUATE ROW("X", [X])', "DEFINE MEASURE is an ad-hoc measure"),
    ]:
        with pytest.raises(CompileError, match=message):
            tiny.run_dax(text, False)


def test_security_applies_to_dax_queries(tiny):
    red = Identity("alice", ["Red Only"])
    assert dax(tiny, 'EVALUATE ROW("T", CALCULATE([Total], ALL(Product)))', identity=red) == [{"[T]": 750}]


@pytest.mark.parametrize("text, message", [
    ("SUMMARIZECOLUMNS(Product[Name])", "starts with EVALUATE"),
    ("EVALUATE Product", "needs a table expression"),
    ("EVALUATE ADDCOLUMNS(Product, \"x\", 1)", "not supported"),
    ('EVALUATE SUMMARIZECOLUMNS(Product[Name], "T")', "pairs"),
    ("EVALUATE { 1, 2 }", "one expression"),
    ('EVALUATE ROW("T", [Total]) EVALUATE ROW("T", [Total])', "one EVALUATE per query"),
    ('EVALUATE ROW("T", [Total]', "expected"),
    ("DEFINE VAR x = 1 EVALUATE { x }", "MEASURE definitions only"),
])
def test_query_errors(text, message):
    with pytest.raises(DaxQueryError, match=message):
        parse_dax_query(text)


def test_order_by_must_name_an_output(tiny):
    with pytest.raises(CompileError, match="can only order by"):
        tiny.run_dax('EVALUATE SUMMARIZECOLUMNS(Product[Name], "T", [Total]) ORDER BY Product[Color]', True)


def test_http_endpoint_answers_in_the_execute_queries_shape(tiny):
    server = make_server(tiny.root, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/dax"

    def post(body):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode())) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        status, data = post({"queries": [
            {"query": 'EVALUATE SUMMARIZECOLUMNS(Product[Category], "Revenue", [Total])'},
            {"query": 'EVALUATE ROW("Revenue", [Total])'},
        ]})
        assert status == 200
        assert data["results"][0]["tables"][0]["rows"][0] == {"Product[Category]": "Bikes", "[Revenue]": 1500}
        assert data["results"][1]["tables"][0]["rows"] == [{"[Revenue]": 1700}]
        # The server runs governed by default: an expression written in the query is refused.
        status, data = post({"query": 'EVALUATE ROW("X", SUM(Sales[Qty]))'})
        assert status == 400 and "ad-hoc" in data["error"]
        status, data = post({"queries": []})
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
