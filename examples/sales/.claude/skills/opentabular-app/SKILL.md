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
