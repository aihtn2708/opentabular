# DAX coverage

What the compiler translates today, and where results can differ from Power BI.
Anything not listed is rejected with an `unsupported` message at validation time.

## Supported

| Area | Functions and syntax |
| --- | --- |
| Aggregation | `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `COUNTA`, `DISTINCTCOUNT`, `DISTINCTCOUNTNOBLANK`, `COUNTROWS` |
| Iterators | `SUMX`, `AVERAGEX`, `MINX`, `MAXX`, `COUNTX` over a table, `FILTER(table, …)` or `ALL(table)`; `RELATED` inside the row expression |
| Level of detail | An iterator or `COUNTROWS` over column values: `VALUES(col)`, `DISTINCT(col)`, `ALL(col, …)`, `SUMMARIZE(table, col, …)`, each optionally inside `FILTER(…, condition on a measure)`. Example: `AVERAGEX(VALUES('Date'[Year Month]), [Total Sales])` |
| Filter context | `CALCULATE` with boolean filters, `KEEPFILTERS`, `ALL`, `REMOVEFILTERS`, `ALLEXCEPT`, `FILTER(table, …)`, `FILTER(ALL(…), …)` |
| Time intelligence | `SAMEPERIODLASTYEAR`, `DATEADD`, `DATESYTD` / `DATESQTD` / `DATESMTD`, `TOTALYTD` / `TOTALQTD` / `TOTALMTD`, `DATESINPERIOD(dates, MAX(dates), -N, unit)`, and the running-total idiom `FILTER(ALL('Date'), 'Date'[Date] <= MAX('Date'[Date]))` |
| Logic | `IF`, `SWITCH` (including `SWITCH(TRUE(), …)`), `AND`, `OR`, `NOT`, `COALESCE`, `ISBLANK`, `BLANK`, `TRUE`, `FALSE` |
| Math | `DIVIDE`, `ABS`, `ROUND`, `INT`, `SQRT`, `POWER`, `MOD`, `EXP`, `LN`, two-argument `MIN` / `MAX` |
| Text and dates | `UPPER`, `LOWER`, `LEN`, `LEFT`, `RIGHT`, `TRIM`, `CONCATENATE`, `CONTAINSSTRING`, `YEAR`, `QUARTER`, `MONTH`, `DAY`, `DATE` |
| Syntax | `VAR … RETURN`, measure references, `IN { … }`, `&`, all arithmetic, comparison and logical operators, comments |

Operator precedence follows <https://dax.guide/operators/>.

## Semantics that are implemented

- **Filter replacement.** A `CALCULATE` filter on a column replaces an existing filter
  on that column, including a group-by on it. `KEEPFILTERS` intersects instead.
- **Expanded tables.** `ALL(Sales)` also removes filters from tables that `Sales`
  reaches through many-to-one relationships.
- **Filter direction.** A column filters a measure only if the measure's base table
  reaches the column's table through many-to-one relationships. Otherwise the value
  repeats, as in Power BI with single-direction relationships.
- **Context transition over column values.** When an iterator runs over the values of a
  column, a measure reference or `CALCULATE` inside it is evaluated once per value, and
  the result is aggregated (an inner query grouped by the outer keys plus the iterated
  columns). A bare aggregation such as `SUM(Sales[Qty])` inside the iterator is not
  filtered by the current value, exactly as in DAX. `CALCULATE` filters apply after the
  transition, so `CALCULATE([M], ALL(Product))` inside the iterator ignores the row.
- **Variables** are evaluated where they are defined, not where they are used.
- **BLANK arithmetic.** `BLANK + 5` is `5`, `BLANK + BLANK` is `BLANK`, `BLANK * 5` is
  `BLANK`. Aggregations over no rows return `BLANK`, including the `COUNT` family.
- **Blank rows are dropped.** A result row is kept only if at least one measure is not
  blank.
- **Unmatched keys.** Fact rows whose key has no dimension row appear under a blank
  dimension member.
- **Time intelligence replaces date filters.** The date table is treated as a marked
  date table: a time function removes the filters on the date table and applies the
  shifted or extended period.

## Known differences from Power BI

| Topic | OpenTabular | Power BI |
| --- | --- | --- |
| Comparison with BLANK | `BLANK = 0` is unknown (SQL `NULL`) | `BLANK = 0` is true |
| Division by zero with `/` | BLANK | Infinity or an error |
| Blank dimension member | Appears when at least one measure in the query is evaluated at the full grain of the group-by; missing when every measure removes a group-by column | Always appears when unmatched fact rows exist |
| 29 February in year shifts | Sales on 28 and 29 Feb of a leap year both map to 28 Feb of the next year | Depends on whether whole months are selected |
| `DISTINCTCOUNT` on a dimension column | Counts blank only when a real row has a blank value | Also counts the blank row added for unmatched keys |
| Text comparison | Case-sensitive, as in the database | Case-insensitive |
| Blank member in column iterators | `VALUES(col)` includes the blank member only when the measure inside has rows for it | Always includes it when unmatched fact rows exist |
| Sort order | By the column itself | By `sortByColumn` when set |

## Not supported yet

- Row-by-row context transition over a table: `SUMX(Sales, [M])`, `SUMX(Customer, [M])`
  (iterate `VALUES` of the key column instead)
- One column iterator directly inside another, and iterating date-table columns inside
  time intelligence
- An aggregated variable used inside an iterator: `VAR t = [M] RETURN SUMX(VALUES(col), [M] / t)`
- Table functions as values elsewhere: `ADDCOLUMNS`, `TOPN`, `CROSSJOIN`, `SUMMARIZE` with
  added columns
- `RANKX`, `SELECTEDVALUE`, `HASONEVALUE`, `ISINSCOPE`, `ALLSELECTED`, `TREATAS`, `LOOKUPVALUE`
- `USERELATIONSHIP`, `CROSSFILTER`, inactive, bidirectional and many-to-many relationships
- `PREVIOUSMONTH`-style functions, `PARALLELPERIOD`, `DATESBETWEEN`, fiscal year ends
- Time intelligence nested inside a to-date or rolling window (the other order works:
  `CALCULATE([Sales YTD], SAMEPERIODLASTYEAR(…))`)
- Calculated columns, calculated tables, calculation groups, DAX user-defined functions
- Row-level and object-level security roles
