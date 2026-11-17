"""The same model on PostgreSQL must give the same numbers as on DuckDB.

Runs only when OTAB_TEST_POSTGRES_DSN points at a PostgreSQL database the
tests may create schemas in, for example:

    OTAB_TEST_POSTGRES_DSN=postgresql://postgres@127.0.0.1:5432/postgres pytest
"""
import datetime as dt
import os
import shutil
from decimal import Decimal

import duckdb
import pytest
import yaml

from opentabular import Project, Query
from opentabular.security import Identity

DSN = os.environ.get("OTAB_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="OTAB_TEST_POSTGRES_DSN is not set")

_PG_TYPES = {"BIGINT": "bigint", "INTEGER": "integer", "DOUBLE": "double precision",
             "DATE": "date", "VARCHAR": "text", "BOOLEAN": "boolean"}


def to_postgres(project: Project, schema: str, tmp_path_factory) -> Project:
    """Copy a DuckDB-on-CSV project into PostgreSQL tables and return a
    project bound to them."""
    import psycopg

    root = tmp_path_factory.mktemp(schema) / "project"
    shutil.copytree(project.root, root)
    config = yaml.safe_load((root / "opentabular.yml").read_text())
    sources = {}
    with psycopg.connect(DSN, autocommit=True) as pg:
        pg.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        pg.execute(f'CREATE SCHEMA "{schema}"')
        for table in project.model.tables.values():
            file = project.root / config["sources"][table.name]
            name = table.entity_name or table.name
            rel = duckdb.sql(f"SELECT * FROM read_csv_auto('{file}')")
            cols = ", ".join(f'"{c}" {_PG_TYPES[str(t)]}' for c, t in zip(rel.columns, rel.types))
            pg.execute(f'CREATE TABLE "{schema}"."{name}" ({cols})')
            marks = ", ".join(["%s"] * len(rel.columns))
            with pg.cursor() as cur:
                cur.executemany(f'INSERT INTO "{schema}"."{name}" VALUES ({marks})', rel.fetchall())
            sources[table.name] = {"table": f"{schema}.{name}"}
    config["connection"] = {"type": "postgres", "dsn_env": "OTAB_TEST_POSTGRES_DSN"}
    config["sources"] = sources
    (root / "opentabular.yml").write_text(yaml.safe_dump(config))
    return Project.load(root)


@pytest.fixture(scope="module")
def pg_tiny(tiny, tmp_path_factory):
    return to_postgres(tiny, "otab_tiny", tmp_path_factory)


@pytest.fixture(scope="module")
def pg_sales(sales, tmp_path_factory):
    return to_postgres(sales, "otab_sales", tmp_path_factory)


def normal(rows):
    def cell(v):
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, dt.datetime):
            return v.date()
        return v
    return sorted(([cell(v) for v in r] for r in rows), key=lambda r: [("" if v is None else str(v)) for v in r])


def assert_same(duck, pg, query, identity=None):
    a = normal(duck.run(query, True, identity).rows)
    b = normal(pg.run(query, True, identity).rows)
    assert len(a) == len(b), (query, len(a), len(b))
    for ra, rb in zip(a, b):
        for va, vb in zip(ra, rb):
            if isinstance(va, float) or isinstance(vb, float):
                assert va == pytest.approx(vb, rel=1e-9, abs=1e-9), (query, ra, rb)
            else:
                assert va == vb, (query, ra, rb)


TINY_SKIP = {"Loop A", "Loop B"}
GROUPINGS = [
    [], ["'Date'[Year]"], ["Product[Category]"], ["'Date'[Year]", "'Date'[MonthNo]"],
    ["Product[Name]"], ["Sales[Channel]", "Product[Color]"], ["Product[Tone]"], ["Sales[Category]"],
]


@pytest.mark.parametrize("group_by", GROUPINGS)
def test_every_tiny_measure_matches_duckdb(tiny, pg_tiny, group_by):
    for m in tiny.model.all_measures():
        if m.name not in TINY_SKIP:
            assert_same(tiny, pg_tiny, Query([m.name], list(group_by)))


def test_filters_ad_hoc_measures_and_security_match(tiny, pg_tiny):
    cases = [
        Query(["Total", "LY", "YTD"], ["'Date'[Year]"], ['Product[Color] IN {"Red"}']),
        Query(["Total"], [], ["'Date'[Date] >= DATE(2024, 2, 1)"]),
        Query(["Total"], ["'Date'[Year]"], ["'Date'[Date] >= DATE('Date'[Year], 2, 1)"]),
        Query(["X = RANKX(ALL(Product[Name]), [Total], , ASC, DENSE)"], ["Product[Name]"]),
        Query(["X = SUMX(TOPN(2, VALUES(Product[Name]), [Total]), [Total])"], ["Product[Category]"]),
        Query(["X = AVERAGEX(VALUES('Date'[MonthNo]), [Total])"], ["'Date'[Year]"]),
        Query(["X = CALCULATE([Total], PARALLELPERIOD('Date'[Date], -1, YEAR))"], ["'Date'[Year]", "'Date'[MonthNo]"]),
        Query(["X = CALCULATE([Total], PREVIOUSMONTH('Date'[Date]))"], ["'Date'[Year]", "'Date'[MonthNo]"]),
        Query(['X = IF([Total] > 500, "high", "low") & " / " & [Qty]'], ["Product[Name]"]),
        Query(["X = SELECTEDVALUE(Product[Category], \"many\")", "H = HASONEVALUE(Product[Color])"], ["Product[Name]"]),
        Query(["X = DIVIDE([Total], [Qty])", "Y = [Total] / [Qty]", "Z = ROUND([Total] / 7, 2)"], ["Product[Category]"]),
        Query([], ["Product[Category]", "Product[Color]"]),
    ]
    for q in cases:
        assert_same(tiny, pg_tiny, q)
    assert_same(tiny, pg_tiny, Query(["Total", "Rank"], ["Product[Name]"]), Identity("alice", ["Red Only"]))
    assert_same(tiny, pg_tiny, Query(["Total"], ["'Date'[Year]"]), Identity("bob", ["Online 2024"]))


@pytest.mark.parametrize("group_by, filters", [
    ([], []),
    (["'Date'[Year]"], []),
    (["Product[Category]", "'Date'[Quarter]"], ["'Date'[Year] = 2024"]),
    (["'Date'[Year Month]"], ['Customer[Country] = "Vietnam"']),
    (["Customer[Country]", "Product[Product]"], ['Sales[Channel] = "Online"']),
])
def test_every_example_measure_matches_duckdb(sales, pg_sales, group_by, filters):
    names = [m.name for m in sales.model.all_measures() if m.name != "Top Product Names"]
    assert_same(sales, pg_sales, Query(names, group_by, filters))


def test_dax_query_source_check_and_metric_tests_on_postgres(pg_sales, pg_tiny):
    from opentabular.testing import run_tests

    rows = pg_tiny.run_dax('EVALUATE SUMMARIZECOLUMNS(Product[Category], "T", [Total]) ORDER BY [T] DESC', True).records()
    assert [r["[T]"] for r in rows] == [1500, 150, 50]
    assert [i for i in pg_sales.validate(check_sources=True) if i.level == "error"] == []
    outcomes = run_tests(pg_sales)
    assert outcomes and all(o.passed for o in outcomes), [o.detail for o in outcomes if not o.passed]
