"""The HTTP server behind `otab serve`, exercised over real sockets."""
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from opentabular.server import make_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def served(tmp_path):
    project = tmp_path / "sales"
    shutil.copytree(ROOT / "examples" / "sales", project)
    server = make_server(project, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", project
    server.shutdown()
    server.server_close()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers.get_content_type(), r.read()


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_pages_and_sdk_are_served(served):
    base, _ = served
    status, ctype, body = get(base + "/")
    assert status == 200 and ctype == "text/html" and b"/sdk.js" in body
    status, ctype, body = get(base + "/sdk.js")
    assert status == 200 and ctype == "text/javascript" and b"window.otab" in body
    status, ctype, body = get(base + "/apps/revenue")
    assert status == 200 and b"Revenue overview" in body


def test_only_app_pages_are_reachable_under_apps(served):
    base, _ = served
    for path in ("/apps/../opentabular.yml", "/apps/%2e%2e/opentabular.yml", "/apps/nope", "/data/sales.csv"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + path)
        assert e.value.code == 404


def test_query_returns_rows_trust_and_sql(served):
    base, _ = served
    status, data = post(base + "/api/query", {
        "measures": ["Total Sales", "Orders"], "groupBy": ["'Date'[Year]"],
        "filters": [{"column": "'Product'[Category]", "op": "=", "value": "Bikes"}],
    })
    assert status == 200
    assert [c["name"] for c in data["columns"]] == ["Year", "Total Sales", "Orders"]
    assert [c["trust"] for c in data["columns"]] == [None, "certified", "governed"]
    assert data["columns"][1]["owner"] == "Finance"
    assert [r[0] for r in data["rows"]] == [2023, 2024] and data["truncated"] is False
    assert "GROUP BY" in data["sql"]


def test_errors_are_json_and_ad_hoc_is_off_by_default(served):
    base, _ = served
    status, data = post(base + "/api/query", {"measures": ["Mine = SUM(Sales[Quantity])"]})
    assert status == 400 and "ad-hoc measures are disabled" in data["error"]
    status, data = post(base + "/api/query", {"measures": ["Top Product Names"]})
    assert status == 400 and data["kind"] == "UnsupportedError"
    status, data = post(base + "/api/query", {"measures": ["Total Sales"], "limit": "many"})
    assert status == 400 and data["kind"] == "request"


def test_catalogue_explain_and_search(served):
    base, _ = served
    model = json.loads(get(base + "/api/model")[2])
    assert model["apps"] == ["revenue"] and model["allow_adhoc"] is False
    info = json.loads(get(base + "/api/measures/Gross%20Margin%20%25")[2])
    assert info["lineage"]["measures"] == ["Gross Margin", "Total Sales", "Total Cost"]
    assert {"table": "Product", "relation": "product",
            "columns": {"'Product'[Unit Cost]": "unit_cost"}} in info["lineage"]["sources"]
    hits = json.loads(get(base + "/api/search?q=monthly")[2])["results"]
    assert hits[0]["name"] == "Avg Monthly Sales"


def test_model_reloads_when_a_tmdl_file_changes(served):
    base, project = served
    _, before = post(base + "/api/query", {"measures": ["Total Quantity"]})
    file = project / "definition" / "tables" / "Sales.tmdl"
    time.sleep(0.01)
    file.write_text(file.read_text().replace(
        "measure 'Total Quantity' = SUM(Sales[Quantity])",
        "measure 'Total Quantity' = SUM(Sales[Quantity]) * 2"))
    _, after = post(base + "/api/query", {"measures": ["Total Quantity"]})
    assert after["rows"][0][0] == before["rows"][0][0] * 2


def test_password_protects_everything_except_the_health_check(tmp_path):
    import base64

    project = tmp_path / "sales"
    shutil.copytree(ROOT / "examples" / "sales", project)
    server = make_server(project, port=0, password="s3cret+/=")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json.loads(get(base + "/healthz")[2]) == {"ok": True}
        for path in ("/", "/sdk.js", "/api/model", "/apps/revenue"):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(base + path)
            assert e.value.code == 401 and "Basic" in e.value.headers["WWW-Authenticate"]
        req = urllib.request.Request(base + "/api/query", b'{"measures":["Orders"]}')
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 401

        def auth(pw):
            return {"Authorization": "Basic " + base64.b64encode(f"otab:{pw}".encode()).decode()}

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/api/model", headers=auth("wrong")))
        assert e.value.code == 401
        with urllib.request.urlopen(urllib.request.Request(base + "/api/model", headers=auth("s3cret+/="))) as r:
            assert r.status == 200 and "measures" in json.loads(r.read())
    finally:
        server.shutdown()
        server.server_close()


def test_serve_refuses_a_public_address_without_a_password():
    from click.testing import CliRunner

    from opentabular.cli import main

    r = CliRunner().invoke(main, ["serve", "-p", str(ROOT / "examples" / "sales"), "--host", "0.0.0.0"],
                           env={"OTAB_PASSWORD": ""})
    assert r.exit_code != 0 and "without a password" in r.output
