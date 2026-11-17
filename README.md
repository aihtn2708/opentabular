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
| **Security** | Row-level security roles written in TMDL apply to every query; each person signs in with their own account |

Every number carries its working: definition, owner, trust level, lineage and the SQL that ran.

> **Status: prototype (0.3).** A documented subset of DAX is compiled. It runs on DuckDB and
> PostgreSQL; Snowflake and BigQuery connections exist but have not been run against live accounts.
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
| `otab dax "EVALUATE …"` | Run a DAX query |
| `otab sql -m … -g … -f … [--dialect]` | Print the compiled SQL |
| `otab test` | Run the metric tests in `tests/*.yml` |
| `otab serve` | Metrics explorer, data apps and HTTP API on http://127.0.0.1:7979/ |
| `otab mcp` | Serve the model to AI clients over MCP (stdio) |
| `otab agent setup` | Write `AGENTS.md` and skills that teach coding agents to model and build apps |
| `otab hash-password` | Make a password hash for a user in `opentabular.yml` |
| `otab functions` | List supported DAX functions |

Filters are DAX conditions on one column: `-f "'Date'[Year] = 2024"`,
`-f 'Product[Color] IN {"Red", "Blue"}'`. `-m` also accepts an ad-hoc measure,
`-m "Units = SUM(Sales[Quantity])"`, for exploration from the CLI. `query`, `sql`, `dax` and
`mcp` take `--role` and `--user` to run as a viewer with that security role.

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

HTTP API: `GET /api/model`, `POST /api/query`, `POST /api/dax`, `GET /api/measures/<name>`,
`GET /api/search?q=`.

## DAX queries

Clients that already write DAX can send a query instead of a list of measures:

```
$ otab dax -p examples/sales 'EVALUATE TOPN(3,
      SUMMARIZECOLUMNS(Product[Product], TREATAS({2024}, Date[Year]), "Revenue", [Total Sales]),
      [Revenue], DESC)'
Product[Product]  [Revenue]
----------------  ---------
Trail 700            35,190
Trail 500            20,709
City Cruiser         11,421
```

Accepted shapes: `SUMMARIZECOLUMNS`, `ROW`, `{ expression }`, `VALUES(column)`, each optionally
inside `TOPN` or `CALCULATETABLE`, with `DEFINE MEASURE` and `ORDER BY`. `POST /api/dax` takes
`{"queries": [{"query": "EVALUATE ..."}]}` and answers in the shape of Power BI's executeQueries
REST API (`results[].tables[].rows[]`). On the server only model measures may be requested
(`"Revenue", [Total Sales]`); expressions written in the query and `DEFINE MEASURE` need
`--allow-adhoc`. This is not the XMLA protocol, so Power BI Desktop and Excel cannot connect to it.

## Security

Roles are part of the model, as in Power BI:

```tmdl
role 'Vietnam Sales'
	modelPermission: read

	tablePermission Customer = Customer[Country] = "Vietnam"
```

Who holds which role is deployment configuration in `opentabular.yml`:

```yaml
security:
  users:
    - name: alice
      password_hash: pbkdf2_sha256$600000$...   # from `otab hash-password`
      roles: [Vietnam Sales]
    - name: owner
      password_hash: pbkdf2_sha256$600000$...
      admin: true                                # no row restriction
```

- A role's filter reaches fact tables through relationships, and is added beneath the DAX,
  so `ALL()`, `REMOVEFILTERS()` and `CALCULATE` cannot remove it.
- Several roles combine as in Power BI: per table, a person sees the rows any of their roles
  allows; a role with no filter on a table allows all of its rows.
- `USERNAME()` in a filter is the signed-in name, for per-person rules.
- A signed-in user with no role sees nothing when the model defines roles.
- Preview a role without accounts: `otab serve --role "Vietnam Sales"`, or
  `otab query --role "Vietnam Sales" -m "Total Sales"`. Metric tests take `roles:` too.

Not covered: object-level security (hiding columns or tables per role) and sign-in through an
identity provider.

## Databases

```yaml
connection:
  type: postgres            # duckdb (default), postgres, snowflake, bigquery
  dsn_env: DATABASE_URL     # name of the environment variable holding the connection string
sources:
  Sales: {table: analytics.fact_sales}
```

The compiler writes DuckDB SQL and translates it for the target database. PostgreSQL is tested:
every measure of the two test models returns the same numbers as on DuckDB. Snowflake and
BigQuery use the same path (`pip install "opentabular[snowflake]"` or `[bigquery]`) but have
not been run against a live account, so expect to fix details. File sources (`.csv`,
`.parquet`) work with DuckDB only.

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

- DuckDB and PostgreSQL are tested. Snowflake and BigQuery connections, and
  `otab sql --dialect` for other databases, produce SQL that has not been executed there.
- A measure can be evaluated row by row over a table that has a key column (a dimension).
  Over a table without one, `SUMX(Sales, [Measure])` is rejected with an "unsupported"
  message, never silently miscompiled.
- Not compiled yet: calculated tables, calculation groups, bidirectional and many-to-many
  relationships, `TOPN` as a `CALCULATE` filter, `ALLSELECTED`, `TREATAS` inside measures,
  `CONCATENATEX`, window functions, object-level security.
- No Power BI engine was available to compare against. Results are checked against
  hand-calculated values, hand-written SQL, and DuckDB against PostgreSQL, not against
  Power BI itself.
- Sign-in is HTTP Basic with accounts in a file. There is no single sign-on.

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
