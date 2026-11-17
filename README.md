# OpenTabular

An open-source semantic layer built on **TMDL** and **DAX**.

OpenTabular reads a semantic model written in TMDL (the text format Power BI and Fabric
use for models), compiles its DAX measures to SQL, and runs them on your own database.
The same model then serves three purposes:

| Trend | What OpenTabular gives you |
| --- | --- |
| **Semantic layer** | One definition of every metric: tables, relationships and DAX measures in `.tmdl` files |
| **Analytics as code** | The model lives in git; `otab validate` and `otab test` check it in CI |
| **Self-service AI** | An MCP server lets AI agents query measures by name; the compiler writes the SQL |
| **Data apps** | `otab serve` gives a metrics explorer and hosts HTML apps that query measures through a small JavaScript client |

Every number carries its working: definition, owner, trust level, lineage and the SQL that ran.

> **Status: prototype (0.2).** A documented subset of DAX is compiled, on DuckDB only.
> "OpenTabular" is a working name. The project is not affiliated with Microsoft.

## Quick start

```bash
pip install -e ".[dev]"

otab validate -p examples/sales          # parse the model, compile every measure
otab query -p examples/sales \
     -m "Total Sales" -m "Sales LY" -m "Sales YoY %" \
     -g "'Date'[Year]"
```

```
Year  Total Sales   Sales LY  Sales YoY %
----  -----------  ---------  -----------
2023    91,869.85
2024    108,265.9  91,869.85       0.1785
```

The measures come from `examples/sales/definition/tables/Sales.tmdl`:

```tmdl
table Sales

	/// Revenue after discount.
	measure 'Total Sales' = SUMX(Sales, Sales[Quantity] * Sales[Unit Price] * (1 - Sales[Discount]))
		formatString: #,##0.00

	/// Revenue in the same period one year earlier.
	measure 'Sales LY' = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))

	/// Revenue growth against the same period one year earlier.
	measure 'Sales YoY %' =
			VAR Prev = [Sales LY]
			RETURN
				DIVIDE([Total Sales] - Prev, Prev)
		formatString: 0.0%
```

`otab sql` prints the SQL instead of running it.

## A project

```
examples/sales/
├── opentabular.yml          # connection + where each table's data lives
├── definition/              # the TMDL model, same layout Power BI writes
│   ├── model.tmdl
│   ├── relationships.tmdl
│   └── tables/Sales.tmdl, Product.tmdl, Customer.tmdl, Date.tmdl
├── tests/metrics.yml        # metric tests
├── apps/revenue.html        # a data app, served at /apps/revenue
└── data/*.csv               # example data
```

```yaml
# opentabular.yml
model: definition
connection:
  type: duckdb
  database: ":memory:"
sources:
  Sales: data/sales.csv                       # a file, exposed as a view
  Product: {table: analytics.dim_product}     # or an existing relation
```

Tables without a `sources` entry are read from the relation named in their TMDL entity
partition (`schemaName`.`entityName`). Column names map through `sourceColumn`.

## Commands

| Command | Purpose |
| --- | --- |
| `otab init [dir]` | Create a starter project: model, data, a metric test, an app, agent instructions |
| `otab validate [--check-sources] [--strict]` | Parse the TMDL, compile every measure, report errors and unsupported DAX. `--check-sources` also checks bound columns against the database |
| `otab describe [--json]` | Catalogue of tables, columns, measures and relationships |
| `otab search <words>` | Find measures and columns by name or description |
| `otab lineage <measure>` | What a measure is built from, down to source columns, and what depends on it |
| `otab query -m … -g … -f …` | Run a semantic query: measures, group-by columns, filters |
| `otab sql -m … -g … -f … [--dialect]` | Print the compiled SQL |
| `otab test` | Run the metric tests in `tests/*.yml` |
| `otab serve` | Metrics explorer, data apps and HTTP API on http://127.0.0.1:7979/ |
| `otab mcp` | Serve the model to AI clients over MCP (stdio) |
| `otab agent setup` | Write `AGENTS.md` and skills that teach coding agents to model and build apps |
| `otab functions` | List supported DAX functions |

Filters are DAX conditions on one column: `-f "'Date'[Year] = 2024"`,
`-f 'Product[Color] IN {"Red", "Blue"}'`. `-m` also accepts an ad-hoc measure,
`-m "Units = SUM(Sales[Quantity])"`, for exploration from the CLI.

## Explorer and data apps

```bash
otab serve -p examples/sales
# Explorer:  http://127.0.0.1:7979/
# App:       http://127.0.0.1:7979/apps/revenue
```

The explorer lets you pick measures and columns, charts the result, and shows how each
number is calculated. A data app is one HTML file in `apps/`. It loads `/sdk.js` and asks
for measures by name:

```html
<div id="trend"></div>
<div id="inspect"></div>
<script src="/sdk.js"></script>
<script>
  (async () => {
    const result = await otab.query({
      measures: ['Total Sales', 'Sales LY'],
      groupBy: ["'Date'[Year Month]"],
      filters: [{ column: "'Date'[Year]", op: '=', value: 2024 }],
    });
    otab.line(document.getElementById('trend'), result);       // also bars, table, tile
    otab.inspect(document.getElementById('inspect'), result);  // definitions, lineage, SQL
  })();
</script>
```

The page never sends SQL or DAX, and ad-hoc measures are refused unless the server is
started with `--allow-adhoc`. The model reloads when a `.tmdl` or `.yml` file changes.
The server binds to localhost by default. To listen on a public address it requires a
shared password (`OTAB_PASSWORD`); see [DEPLOY.md](DEPLOY.md) for a free hosted test
deployment.

HTTP API: `GET /api/model`, `POST /api/query`, `GET /api/measures/<name>`,
`GET /api/search?q=`.

## Trust and lineage

Each measure in a result is labelled:

| Level | Meaning |
| --- | --- |
| certified | Defined in the model, and marked `annotation Endorsement = Certified` |
| governed | Defined in the model, so reviewed with it |
| ad hoc | Written in the query; not in the model |

An owner is recorded with `annotation Owner = Finance` on the measure. Both annotations
are plain TMDL, so Power BI keeps them when it round-trips the model. Lineage is derived
from the DAX itself:

```
$ otab lineage -p examples/sales "Gross Margin %"
[Gross Margin %]  trust: governed  owner: Finance
Built from measures: [Gross Margin], [Total Sales], [Total Cost]
Columns
  'Sales'[Quantity]  <-  sales.quantity
  'Product'[Unit Cost]  <-  product.unit_cost
  ...
```

## Analytics as code

Metric tests pin the result of a semantic query, so a change that moves a number fails
the build:

```yaml
# tests/metrics.yml
tests:
  - name: revenue and orders by year
    query:
      measures: [Total Sales, Orders]
      group_by: ["'Date'[Year]"]
    expect:
      tolerance: 0.005
      rows:
        - {Year: 2023, Total Sales: 91869.85, Orders: 205}
        - {Year: 2024, Total Sales: 108265.90, Orders: 215}
```

`.github/workflows/ci.yml` runs `otab validate`, `otab test` and the unit tests on every
pull request.

## Self-service AI

`otab mcp -p <project>` starts an MCP server with four tools:

- `describe_model`: tables, columns, measures and their descriptions (`///` comments in TMDL)
- `search_model`: find measures and columns by words
- `query`: measures by name, grouped and filtered by model columns; returns rows, each
  measure's trust level and owner, and the SQL
- `explain_measure`: the DAX behind a number, its owner, and its lineage

The agent never writes SQL or DAX, and ad-hoc measures are rejected on this interface, so
every number it reports comes from a definition in the repository. Example client
configuration:

```json
{
  "mcpServers": {
    "sales-model": {
      "command": "otab",
      "args": ["mcp", "--project", "/path/to/examples/sales"]
    }
  }
}
```

For coding agents that edit the repository (Claude Code, Codex, Cursor), `otab agent setup`
writes an `AGENTS.md` and two skills: one for modelling in TMDL and DAX, one for building
data apps. They tell the agent to put every metric in the model, to run `otab validate`
and `otab test` after each change, and to include an inspect panel in every app.

## Python API

```python
from opentabular import Project, Query

project = Project.load("examples/sales")
result = project.run(Query(
    measures=["Total Sales", "Gross Margin %"],
    group_by=["Product[Category]"],
    filters=["'Date'[Year] = 2024"],
))
print(result.columns, result.rows)
print(result.sql)
```

## How DAX becomes SQL

A semantic query behaves like `SUMMARIZECOLUMNS`: group-by columns and filters form the
filter context, and each measure is evaluated in it.

1. The compiler walks each measure while tracking the filter context. `CALCULATE`
   produces a modified context for the expression inside it.
2. Each aggregation becomes a *leaf*: one aggregate, over one base table, under one
   context. Identical leaves are shared across measures.
3. Each leaf compiles to one `SELECT … GROUP BY` over the base table joined to the
   dimensions it needs. Joins are many-to-one only, so rows never fan out.
4. Leaves are joined on the group-by keys they share, and the measure arithmetic runs on
   top. A leaf that removed a filter, such as `ALL(Product)`, has fewer keys, which is
   how a share-of-total is computed.

Time intelligence joins each fact date to the dates of the outer context, shifted or as
a range. Iterating the values of a column, as in
`AVERAGEX(VALUES('Date'[Year Month]), [Total Sales])`, compiles to an inner query at the
finer grain that is then aggregated to the grain of the report. See [docs/dax-coverage.md](docs/dax-coverage.md) for the supported functions
and for the places where results can differ from Power BI.

## Limits of the prototype

- DuckDB is the only engine that runs queries. `otab sql --dialect` translates the SQL
  for PostgreSQL, Snowflake, BigQuery, Databricks, Redshift, ClickHouse and MySQL, but
  that output has only been checked to parse; it has not been run on those databases.
- Context transition works over the values of columns. Row by row over a table,
  `SUMX(Sales, [Measure])`, is rejected with an "unsupported" message, never silently
  miscompiled.
- Not compiled yet: calculated columns and tables, calculation groups, `USERELATIONSHIP`,
  bidirectional and many-to-many relationships, row-level security, `RANKX`, `TOPN`.
- No Power BI engine was available to compare against. Results are checked against
  hand-calculated values and hand-written SQL, not against Power BI itself.
- `otab serve` has one optional shared password; it has no users, roles or permissions.

## Related projects

[anfra](https://anfra.ai/) is an open-source BI engine from the Holistics team with its
own modelling and query languages (AML and AQL), a JavaScript SDK, an MCP server and
eight supported warehouses. It is far more complete than this prototype. OpenTabular
exists for teams whose metrics are already written in DAX and TMDL: it reads those files
as they are. [Sidemantic](https://github.com/sidequery/sidemantic) imports TMDL among
many other formats.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Licence

Apache-2.0. DAX, TMDL, Power BI and Microsoft Fabric are Microsoft technologies; this is
an independent implementation.
