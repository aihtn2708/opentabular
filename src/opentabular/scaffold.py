"""`otab init` and `otab agent setup`: project scaffold and instructions for
coding agents (Claude Code, Codex, Cursor and others that read AGENTS.md)."""
from __future__ import annotations

from pathlib import Path

from .compiler import SUPPORTED_FUNCTIONS

T = "\t"

_FILES: dict[str, str] = {
    "opentabular.yml": """\
# OpenTabular project file
model: definition            # folder of .tmdl files (the semantic model)

connection:
  type: duckdb
  database: ":memory:"

# Bind each model table to its data: a .csv / .parquet file, or
# {table: schema.name} for a relation that already exists in the database.
sources:
  Orders: data/orders.csv
""",
    "definition/model.tmdl": f"""\
model Model
{T}culture: en-US

ref table Orders
""",
    "definition/relationships.tmdl": f"""\
// Add one block per relationship, from the many side to the one side:
//
// relationship orders-to-customer
// {T}fromColumn: Orders.'Customer Key'
// {T}toColumn: Customer.'Customer Key'
""",
    "definition/tables/Orders.tmdl": f"""\
/// One row per order.
table Orders

{T}/// Order value, summed.
{T}measure Revenue = SUM(Orders[Amount])
{T}{T}formatString: #,##0.00
{T}{T}displayFolder: Revenue

{T}{T}annotation Owner = Analytics

{T}/// Number of orders.
{T}measure 'Order Count' = COUNTROWS(Orders)
{T}{T}formatString: #,##0
{T}{T}displayFolder: Volume

{T}/// Revenue per order.
{T}measure 'Average Order Value' = DIVIDE([Revenue], [Order Count])
{T}{T}formatString: #,##0.00
{T}{T}displayFolder: Revenue

{T}column 'Order ID'
{T}{T}dataType: int64
{T}{T}sourceColumn: order_id
{T}{T}summarizeBy: none

{T}column 'Order Date'
{T}{T}dataType: dateTime
{T}{T}sourceColumn: order_date
{T}{T}summarizeBy: none

{T}/// Sales region of the customer.
{T}column Region
{T}{T}dataType: string
{T}{T}sourceColumn: region
{T}{T}summarizeBy: none

{T}column Amount
{T}{T}dataType: double
{T}{T}sourceColumn: amount
{T}{T}summarizeBy: sum

{T}partition Orders = entity
{T}{T}mode: directLake
{T}{T}source
{T}{T}{T}entityName: orders
{T}{T}{T}expressionSource: DatabaseQuery
""",
    "data/orders.csv": """\
order_id,order_date,region,amount
1,2026-01-05,North,120.00
2,2026-01-09,South,80.50
3,2026-01-17,North,210.00
4,2026-02-02,East,95.25
5,2026-02-11,South,310.00
6,2026-02-20,North,60.00
7,2026-03-03,East,150.75
8,2026-03-15,South,45.00
""",
    "tests/metrics.yml": """\
# Metric tests: `otab test` fails when a pinned number changes.
tests:
  - name: revenue by region
    query:
      measures: [Revenue, Order Count]
      group_by: ["Orders[Region]"]
    expect:
      rows:
        - {Region: East, Revenue: 246.00, Order Count: 2}
        - {Region: North, Revenue: 390.00, Order Count: 3}
        - {Region: South, Revenue: 435.50, Order Count: 3}
""",
    "apps/overview.html": """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Overview</title>
<style>
  body { font: 14px/1.45 system-ui, sans-serif; max-width: 760px; margin: 32px auto; padding: 0 20px; }
  h1 { font-size: 22px; } h2 { font-size: 15px; margin-top: 28px; }
</style>
</head>
<body>
  <h1>Overview</h1>
  <div id="total"></div>
  <h2>Revenue by region</h2>
  <div id="regions"></div>
  <h2>How these numbers are calculated</h2>
  <div id="inspect"></div>
  <script src="/sdk.js"></script>
  <script>
    (async () => {
      const total = await otab.query({ measures: ['Revenue'] });
      otab.tile(document.getElementById('total'),
        { label: 'Revenue', value: total.rows[0][0], format: total.columns[0].format });
      const regions = await otab.query({
        measures: ['Revenue'], groupBy: ['Orders[Region]'], orderBy: ['Revenue desc'] });
      otab.bars(document.getElementById('regions'), regions);
      otab.inspect(document.getElementById('inspect'), regions);
    })();
  </script>
</body>
</html>
""",
    ".gitignore": "*.duckdb\n__pycache__/\n",
}


def init_project(target: Path) -> list[Path]:
    """Create a starter project. Refuses to overwrite an existing project."""
    if (target / "opentabular.yml").exists():
        raise FileExistsError(f"{target} already contains an OpenTabular project")
    written = []
    for rel, content in _FILES.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    written += agent_setup(target)
    return written


def _agents_md() -> str:
    functions = ", ".join(SUPPORTED_FUNCTIONS)
    return f"""\
# Working in this OpenTabular project

This repository is a semantic model. Metrics are DAX measures in TMDL files,
compiled to SQL by `otab`. Apps and AI clients ask for measures by name.

## Rules

1. A number shown to anyone must come from a measure in `definition/`. Do not
   compute metrics in app code or in ad-hoc SQL. If no measure fits, add one.
2. After every change run `otab validate --check-sources` and `otab test`.
   Fix every `error`. An `unsupported` line means the DAX is valid but this
   compiler cannot translate it yet: rewrite the measure with supported
   functions, or tell the user it cannot be done yet.
3. Give every measure a `///` description and a `formatString`. Descriptions
   are what people and AI clients use to choose a measure.
4. When a measure's number is meant to stay fixed for known data, pin it with
   a test in `tests/*.yml`.
5. Never edit files under `data/` to make a test pass.

## Layout

- `opentabular.yml`: connection, and where each table's data lives
- `definition/tables/<Table>.tmdl`: columns, measures and the partition of one table
- `definition/relationships.tmdl`: many-to-one relationships
- `tests/*.yml`: metric tests
- `apps/<name>.html`: data apps, served at `/apps/<name>` by `otab serve`

## TMDL in one example

Indent with tabs. Properties sit one level under their object; a multi-line
expression sits two levels under it.

```tmdl
table Sales

{T}/// Revenue after discount.
{T}measure 'Total Sales' = SUMX(Sales, Sales[Quantity] * Sales[Unit Price])
{T}{T}formatString: #,##0.00
{T}{T}displayFolder: Revenue

{T}{T}annotation Owner = Finance

{T}measure 'Sales YoY %' =
{T}{T}{T}VAR Prev = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))
{T}{T}{T}RETURN DIVIDE([Total Sales] - Prev, Prev)
{T}{T}formatString: 0.0%

{T}column Quantity
{T}{T}dataType: int64
{T}{T}sourceColumn: quantity
```

`annotation Owner = <team>` names who answers for a measure.
`annotation Endorsement = Certified` marks it as certified; only add it when
the user says the owner has approved the definition.

## Supported DAX

{functions}

Not supported yet: a measure evaluated row by row over a table that has no
key column (`SUMX(Sales, [Measure])`; iterate a dimension or `VALUES` of a
column instead), `TOPN` as a CALCULATE filter, `ALLSELECTED`, `CONCATENATEX`,
calculated tables, calculation groups.

Security roles are TMDL too: `role 'Name'` with `tablePermission Table = <DAX
condition>`. Check a role with `otab query --role "Name" -m "Measure"`.

## Commands

- `otab validate --check-sources`: parse the model and compile every measure
- `otab test`: run the metric tests
- `otab query -m "Measure" -g "Table[Column]" -f "Table[Column] = value"`: run a query
- `otab sql ...`: print the SQL for the same query
- `otab lineage "Measure"`: what a measure is built from and what uses it
- `otab search <words>`: find measures and columns
- `otab serve`: explorer at http://127.0.0.1:7979/ and the data apps

## Building a data app

An app is one HTML file in `apps/`. Load `/sdk.js`, then:

```js
const result = await otab.query({{
  measures: ['Total Sales', 'Sales LY'],
  groupBy: ["'Date'[Year Month]"],
  filters: [{{ column: "'Date'[Year]", op: '=', value: 2024 }}],
  orderBy: ['Total Sales desc'],
}});
otab.line(el, result);      // also: otab.bars, otab.table, otab.tile
otab.inspect(el, result);   // definitions, owners, lineage and SQL
```

Every app must include `otab.inspect` for the measures it shows, so readers
can see how each number is calculated.
"""


_SKILL_MODEL = """\
---
name: opentabular-model
description: Add or change measures, columns, tables and relationships in this OpenTabular semantic model (TMDL files with DAX measures). Use when asked for a new metric, KPI or dimension, or to fix a number.
---

Read `AGENTS.md` at the repository root first; it has the TMDL rules and the
supported DAX.

1. Find what exists: `otab search <words>` and `otab lineage "<measure>"`.
   Build on existing measures instead of repeating their logic.
2. Edit the table's file in `definition/tables/`. Use tabs. Add a `///`
   description and a `formatString`.
3. Run `otab validate --check-sources`. Fix errors. If a measure is reported
   as `unsupported`, rewrite it with supported functions or report the limit.
4. Check the number: `otab query -m "<measure>"` with a group-by, and compare
   it with a figure the user trusts or with a simple SQL query on the source.
5. Pin it in `tests/*.yml` and run `otab test`.
"""

_SKILL_APP = """\
---
name: opentabular-app
description: Build or change a data app or dashboard page on this OpenTabular model. Use when asked for a dashboard, report page or chart backed by the model's measures.
---

Read `AGENTS.md` at the repository root first.

1. List what the model offers: `otab describe`. Use only measures that exist;
   if one is missing, add it with the opentabular-model skill first.
2. Create `apps/<name>.html`. Load `/sdk.js` and query with `otab.query`.
   Never write SQL or DAX in the page, and never compute a metric in
   JavaScript from other numbers.
3. Draw with `otab.tile`, `otab.line`, `otab.bars` and `otab.table`. Put
   filters in one row above the charts. One chart, one axis.
4. Include `otab.inspect` so each number shows its definition and SQL.
5. Run `otab serve` and open `http://127.0.0.1:7979/apps/<name>` to check it.
"""


def agent_setup(target: Path) -> list[Path]:
    """Write AGENTS.md and two agent skills into the project."""
    files = {
        "AGENTS.md": _agents_md(),
        ".claude/skills/opentabular-model/SKILL.md": _SKILL_MODEL,
        ".claude/skills/opentabular-app/SKILL.md": _SKILL_APP,
    }
    written = []
    for rel, content in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
