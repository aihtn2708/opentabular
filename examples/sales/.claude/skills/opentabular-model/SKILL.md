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
