"""Row-level security, checked by hand on the tiny data set (see test_compiler.py).

Roles in the fixture:
    Red Only     Product[Color] = "Red"            -> products A and C
    Bikes Only   Product[Category] = "Bikes"       -> products A and B
    Online 2024  Sales[Channel] = "Online", 'Date'[Year] = 2024
    By User      Product[Name] = USERNAME()
"""
import base64
import json
import shutil
import threading
import urllib.error
import urllib.request

import pytest

from opentabular import CompileError, Project, Query
from opentabular.security import Identity, hash_password, verify_password
from opentabular.server import make_server


def run(project, measures, group_by=(), filters=(), roles=(), user=None):
    identity = Identity(user, list(roles)) if roles or user else None
    return project.run(Query(list(measures), list(group_by), list(filters)), True, identity).rows


def test_a_role_filters_facts_through_the_relationship(tiny):
    assert run(tiny, ["Total"], roles=["Red Only"]) == [(750,)]            # A 600 + C 150
    assert run(tiny, ["Total"], ["Product[Category]"], roles=["Red Only"]) == [("Bikes", 600), ("Parts", 150)]
    # Rows of the secured table itself, and the list of its values, are filtered too.
    assert run(tiny, ["Product Count"], roles=["Red Only"]) == [(2,)]
    assert run(tiny, [], ["Product[Name]"], roles=["Red Only"]) == [("A",), ("C",)]
    # The sale with no product is hidden: nothing proves the viewer may see it.
    assert run(tiny, ["Qty"], roles=["Red Only"]) == [(21,)]


def test_dax_cannot_remove_a_security_filter(tiny):
    for dax in ("X = CALCULATE([Total], ALL(Product))",
                "X = CALCULATE([Total], REMOVEFILTERS())",
                'X = CALCULATE([Total], Product[Color] = "Blue") + 0',
                "X = SUMX(ALL(Sales), Sales[Qty] * Sales[Price])",
                "X = MAXX(ALL(Product[Name]), [Total]) + 0"):
        (value,), = run(tiny, [dax], roles=["Red Only"])
        assert value in (750, 600, 0), dax        # never the 900 of the blue product B
    assert run(tiny, ["Share of Products"], roles=["Red Only"]) == [(1.0,)]
    ranks = run(tiny, ["Rank"], ["Product[Name]"], roles=["Red Only"])
    assert ranks == [("A", 1), ("C", 2)]


def test_roles_combine_as_a_union_per_table(tiny):
    # Red or Bikes: A, B and C.
    assert run(tiny, ["Total"], roles=["Red Only", "Bikes Only"]) == [(1650,)]
    # Each role leaves the other's tables open, so together they restrict nothing.
    assert run(tiny, ["Total"], roles=["Red Only", "Online 2024"]) == [(1700,)]


def test_filters_on_several_tables_all_apply_including_time_intelligence(tiny):
    assert run(tiny, ["Total"], roles=["Online 2024"]) == [(950,)]         # 300 + 600 + 50
    assert run(tiny, ["Total"], ["'Date'[Year]"], roles=["Online 2024"]) == [(2024, 950)]
    # Last year's dates are outside the viewer's rows, so there is nothing to show.
    assert run(tiny, ["LY"], ["'Date'[Year]"], roles=["Online 2024"]) == []
    assert run(tiny, ["Product Count"], roles=["Online 2024"]) == [(4,)]


def test_username_makes_a_role_personal(tiny):
    assert run(tiny, ["Total"], roles=["By User"], user="B") == [(900,)]
    assert run(tiny, ["Total"], roles=["By User"], user="nobody") == []
    with pytest.raises(CompileError, match="needs a signed-in user"):
        run(tiny, ["Total"], roles=["By User"])


def test_unknown_or_missing_roles_are_refused(tiny, sales):
    with pytest.raises(CompileError, match="no role named"):
        run(tiny, ["Total"], roles=["Nope"])
    with pytest.raises(CompileError, match="no role named"):
        run(sales, ["Total Sales"], roles=["Nope"])        # a model without roles
    with pytest.raises(CompileError, match="holds none of them"):
        tiny.run(Query(["Total"]), identity=Identity("eve"))
    assert tiny.run(Query(["Total"]), identity=Identity("root", admin=True)).rows == [(1700,)]
    assert not [i for i in tiny.validate() if i.where.startswith("role")]


def test_password_hashing():
    stored = hash_password("correct horse", iterations=1000)
    assert stored.startswith("pbkdf2_sha256$1000$")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("correct horse", "not-a-hash")


def test_each_signed_in_user_sees_their_own_rows(tiny, tmp_path):
    root = tmp_path / "secured"
    shutil.copytree(tiny.root, root)
    h = lambda pw: hash_password(pw, iterations=1000)  # noqa: E731
    with open(root / "opentabular.yml", "a") as f:
        f.write(
            "security:\n  users:\n"
            f"    - {{name: alice, password_hash: '{h('a-pass')}', roles: [Red Only]}}\n"
            f"    - {{name: bob, password_hash: '{h('b-pass')}', roles: [By User]}}\n"
            f"    - {{name: owner, password_hash: '{h('o-pass')}', admin: true}}\n"
            f"    - {{name: eve, password_hash: '{h('e-pass')}'}}\n"
        )
    server = make_server(root, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def ask(user, pw, path="/api/query", body={"measures": ["Total"]}):
        headers = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data, headers)) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        assert ask("alice", "a-pass")[1]["rows"] == [[750]]
        assert ask("owner", "o-pass")[1]["rows"] == [[1700]]
        assert ask("bob", "b-pass")[1]["rows"] == []           # no product is named "bob"
        assert ask("alice", "wrong")[0] == 401
        assert ask("mallory", "a-pass")[0] == 401
        status, data = ask("eve", "e-pass")                    # signed in, but holds no role
        assert status == 400 and "holds none of them" in data["error"]
        status, model = ask("alice", "a-pass", "/api/model", None)
        assert model["viewer"] == {"name": "alice", "roles": ["Red Only"], "admin": False}
        assert model["roles"] == ["Red Only", "Bikes Only", "Online 2024", "By User"]
    finally:
        server.shutdown()
        server.server_close()


def test_preview_role_and_metric_tests_as_a_viewer(tiny, tmp_path):
    from opentabular.testing import run_tests

    server = make_server(tiny.root, port=0, preview=Identity("preview", ["Bikes Only"]))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/query",
                                     b'{"measures": ["Total"]}')
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["rows"] == [[1500]]
    finally:
        server.shutdown()
        server.server_close()
    spec = tmp_path / "rls.yml"
    spec.write_text(
        "tests:\n"
        "  - name: red viewers see red products only\n"
        "    roles: [Red Only]\n"
        "    query: {measures: [Total]}\n"
        "    expect: {rows: [{Total: 750}]}\n"
    )
    (outcome,) = run_tests(tiny, [spec])
    assert outcome.passed, outcome.detail
