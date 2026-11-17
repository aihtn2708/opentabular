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

	/// Revenue after discount.
	measure 'Total Sales' = SUMX(Sales, Sales[Quantity] * Sales[Unit Price])
		formatString: #,##0.00
		displayFolder: Revenue

		annotation Owner = Finance

	measure 'Sales YoY %' =
			VAR Prev = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))
			RETURN DIVIDE([Total Sales] - Prev, Prev)
		formatString: 0.0%

	column Quantity
		dataType: int64
		sourceColumn: quantity
```

`annotation Owner = <team>` names who answers for a measure.
`annotation Endorsement = Certified` marks it as certified; only add it when
the user says the owner has approved the definition.

## Supported DAX

ABS, ALL, ALLEXCEPT, AND, AVERAGE, AVERAGEX, BLANK, CALCULATE, COALESCE, CONCATENATE, CONTAINSSTRING, COUNT, COUNTA, COUNTROWS, COUNTX, DATE, DATEADD, DATESINPERIOD, DATESMTD, DATESQTD, DATESYTD, DAY, DISTINCT, DISTINCTCOUNT, DISTINCTCOUNTNOBLANK, DIVIDE, EXP, FALSE, FILTER, IF, INT, ISBLANK, KEEPFILTERS, LEFT, LEN, LN, LOWER, MAX, MAXX, MIN, MINX, MOD, MONTH, NOT, OR, POWER, QUARTER, RELATED, REMOVEFILTERS, RIGHT, ROUND, SAMEPERIODLASTYEAR, SQRT, SUM, SUMMARIZE, SUMX, SWITCH, TOTALMTD, TOTALQTD, TOTALYTD, TRIM, TRUE, UPPER, VALUES, YEAR

Not supported yet: a measure referenced row by row inside an iterator over a
table (`SUMX(Sales, [Measure])`), calculated columns, `USERELATIONSHIP`,
`RANKX`, `TOPN`, `SELECTEDVALUE`. Iterating the values of a column is
supported: `AVERAGEX(VALUES('Date'[Year Month]), [Total Sales])`.

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
const result = await otab.query({
  measures: ['Total Sales', 'Sales LY'],
  groupBy: ["'Date'[Year Month]"],
  filters: [{ column: "'Date'[Year]", op: '=', value: 2024 }],
  orderBy: ['Total Sales desc'],
});
otab.line(el, result);      // also: otab.bars, otab.table, otab.tile
otab.inspect(el, result);   // definitions, owners, lineage and SQL
```

Every app must include `otab.inspect` for the measures it shows, so readers
can see how each number is calculated.
