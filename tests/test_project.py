import json

from click.testing import CliRunner

from opentabular import Project
from opentabular.cli import main
from opentabular.testing import run_tests

EXAMPLE = "examples/sales"


def test_validate_reports_unsupported_and_errors(sales, tmp_path):
    issues = sales.validate(check_sources=True)
    assert [(i.level, i.where) for i in issues] == [("unsupported", "[Product Rank]")]

    (tmp_path / "m.tmdl").write_text(
        "table T\n"
        "\tmeasure Good = SUM(T[x])\n"
        "\tmeasure Bad = SUM(T[missing])\n"
        "\tmeasure Broken = SUM(\n"
        "\tcolumn x\n"
        "\t\tdataType: int64\n"
    )
    found = {(i.level, i.where) for i in Project.load(tmp_path).validate()}
    assert ("error", "[Bad]") in found and ("error", "[Broken]") in found
    assert ("info", "[Good]") in found          # no description
    assert ("error", "[Good]") not in found


def test_check_sources_finds_missing_columns(tmp_path):
    (tmp_path / "definition").mkdir()
    (tmp_path / "definition" / "t.tmdl").write_text(
        "table T\n\tcolumn A\n\t\tsourceColumn: a\n\tcolumn B\n\t\tsourceColumn: nope\n"
    )
    (tmp_path / "t.csv").write_text("a\n1\n")
    (tmp_path / "opentabular.yml").write_text("sources:\n  T: t.csv\n")
    issues = Project.load(tmp_path).validate(check_sources=True)
    assert [i.where for i in issues if i.level == "error"] == ["'T'[B]"]


def test_metric_tests_pass_and_detect_a_wrong_number(sales, tmp_path):
    outcomes = run_tests(sales)
    assert outcomes and all(o.passed for o in outcomes), [o.detail for o in outcomes if not o.passed]

    bad = tmp_path / "bad.yml"
    bad.write_text(
        "tests:\n"
        "  - name: wrong on purpose\n"
        "    query: {measures: [Total Quantity]}\n"
        "    expect: {rows: [{Total Quantity: 1}]}\n"
    )
    (outcome,) = run_tests(sales, [bad])
    assert not outcome.passed and "expected 1" in outcome.detail


def test_cli_commands():
    runner = CliRunner()
    r = runner.invoke(main, ["validate", "-p", EXAMPLE])
    assert r.exit_code == 0 and "0 errors, 1 unsupported" in r.output
    r = runner.invoke(main, ["validate", "-p", EXAMPLE, "--strict"])
    assert r.exit_code == 1

    r = runner.invoke(main, ["query", "-p", EXAMPLE, "-m", "Total Quantity", "-g", "Product[Category]", "--json"])
    assert r.exit_code == 0
    assert [row["Category"] for row in json.loads(r.output)] == ["Accessories", "Bikes", "Clothing"]

    r = runner.invoke(main, ["sql", "-p", EXAMPLE, "-m", "Sales YTD", "-g", "'Date'[Year]"])
    assert r.exit_code == 0 and "date_trunc('year'" in r.output

    r = runner.invoke(main, ["query", "-p", EXAMPLE, "-m", "No Such Measure"])
    assert r.exit_code != 0 and "unknown measure" in r.output

    r = runner.invoke(main, ["test", "-p", EXAMPLE])
    assert r.exit_code == 0 and "0 failed" in r.output

    r = runner.invoke(main, ["describe", "-p", EXAMPLE, "--json"])
    assert {m["name"] for m in json.loads(r.output)["measures"]} >= {"Total Sales", "Sales YTD"}


def test_init_creates_a_project_that_validates_and_passes_its_tests(tmp_path):
    runner = CliRunner()
    target = tmp_path / "new"
    r = runner.invoke(main, ["init", str(target)])
    assert r.exit_code == 0, r.output
    project = Project.load(target)
    assert project.validate(check_sources=True) == []
    outcomes = run_tests(project)
    assert outcomes and all(o.passed for o in outcomes)
    assert (target / "AGENTS.md").read_text().count("otab validate") >= 2
    assert (target / ".claude" / "skills" / "opentabular-model" / "SKILL.md").exists()
    # A second init must not overwrite the project.
    r = runner.invoke(main, ["init", str(target)])
    assert r.exit_code != 0 and "already contains" in r.output


def test_lineage_and_search_commands():
    runner = CliRunner()
    r = runner.invoke(main, ["lineage", "-p", EXAMPLE, "Sales YoY %"])
    assert r.exit_code == 0
    assert "Built from measures: [Sales LY], [Total Sales]" in r.output
    assert "'Date'[Date]  <-  date.date" in r.output
    r = runner.invoke(main, ["search", "-p", EXAMPLE, "online"])
    assert r.output.splitlines()[0].startswith("measure  [Online Sales]")


def test_sql_can_be_translated_for_other_databases(sales):
    pytest = __import__("pytest")
    pytest.importorskip("sqlglot")
    from opentabular import Query
    from opentabular.dialects import DIALECTS, DialectError, transpile

    names = [m.name for m in sales.model.all_measures() if m.name != "Product Rank"]
    sql = sales.compile(Query(names, ["'Date'[Year Month]", "Product[Category]"])).sql
    assert transpile(sql, "duckdb") == sql
    for dialect in DIALECTS[1:]:
        assert "SELECT" in transpile(sql, dialect)       # translated and parsed back
    with pytest.raises(DialectError, match="unknown dialect"):
        transpile(sql, "oracle9")
