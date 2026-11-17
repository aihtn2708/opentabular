"""Experimental: translate the compiled SQL to other databases.

The compiler emits DuckDB SQL. This module rewrites it for another engine with
sqlglot. The output is checked to parse in the target dialect, but it has NOT
been run on those databases: treat it as a starting point, not as supported.
Only DuckDB can be used as a project connection today.
"""
from __future__ import annotations

DIALECTS = ("duckdb", "postgres", "snowflake", "bigquery", "databricks", "redshift", "clickhouse", "mysql")


class DialectError(Exception):
    pass


def transpile(sql: str, dialect: str) -> str:
    dialect = dialect.lower()
    if dialect == "duckdb":
        return sql
    if dialect not in DIALECTS:
        raise DialectError(f"unknown dialect {dialect!r}; choose one of: {', '.join(DIALECTS)}")
    try:
        import sqlglot
        from sqlglot.errors import SqlglotError
    except ImportError:
        raise DialectError(
            "translating SQL needs sqlglot: pip install 'opentabular[dialects]'"
        ) from None
    try:
        (out,) = sqlglot.transpile(sql, read="duckdb", write=dialect, pretty=True)
        sqlglot.parse_one(out, read=dialect)  # must at least parse in the target
    except SqlglotError as e:
        raise DialectError(f"could not translate this query to {dialect}: {e}") from None
    return out
