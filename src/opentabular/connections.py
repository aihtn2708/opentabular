"""Database engines a project can run on.

The compiler always emits DuckDB SQL. For another database the SQL is
translated with sqlglot just before it runs.

    connection:
      type: duckdb | postgres | snowflake | bigquery
      # postgres
      dsn_env: DATABASE_URL        # name of an environment variable (preferred), or
      dsn: postgresql://user@host/db
      # snowflake: any snowflake.connector.connect() argument; *_env reads a variable
      account: xy12345
      user: ANALYST
      password_env: SNOWFLAKE_PASSWORD
      warehouse: WH
      database: ANALYTICS
      # bigquery
      project: my-gcp-project

Status: DuckDB and PostgreSQL are covered by the test suite. Snowflake and
BigQuery follow the same path but have not been run against a live account.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

FILE_READERS = {".csv": "read_csv_auto", ".tsv": "read_csv_auto", ".parquet": "read_parquet",
                ".json": "read_json_auto"}


class ConnectionError_(Exception):
    """A connection could not be configured or opened."""


def _setting(cfg: dict, key: str) -> Any:
    """Read `key`, or the environment variable named by `key_env`."""
    if cfg.get(f"{key}_env"):
        name = str(cfg[f"{key}_env"])
        if name not in os.environ:
            raise ConnectionError_(f"connection.{key}_env: environment variable {name} is not set")
        return os.environ[name]
    return cfg.get(key)


class Engine:
    dialect = "duckdb"
    raw: Any = None

    def translate(self, sql: str) -> str:
        if self.dialect == "duckdb":
            return sql
        from .dialects import transpile

        return transpile(sql, self.dialect)

    def query(self, sql: str) -> list[tuple]:
        """Run SQL produced by the compiler and return the rows."""
        return self._run(self.translate(sql))[1]

    def columns(self, relation: str) -> set[str]:
        """Column names of a relation, case-folded."""
        names, _ = self._run(self.translate(f"SELECT * FROM {relation} LIMIT 0"))
        return {n.casefold() for n in names}

    def _run(self, sql: str) -> tuple[list[str], list[tuple]]:
        raise NotImplementedError

    def close(self) -> None:
        if self.raw is not None:
            self.raw.close()


class DuckDBEngine(Engine):
    dialect = "duckdb"

    def __init__(self, cfg: dict, root: Path, file_sources: dict[str, Path]):
        import duckdb

        database = str(cfg.get("database", ":memory:"))
        if database != ":memory:":
            database = str((root / database).resolve())
        self.raw = duckdb.connect(database)
        for view, file in file_sources.items():
            reader = FILE_READERS[file.suffix.lower()]
            path_sql = "'" + str(file).replace("'", "''") + "'"
            name = '"' + view.replace('"', '""') + '"'
            self.raw.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {reader}({path_sql})")

    def _run(self, sql: str) -> tuple[list[str], list[tuple]]:
        cur = self.raw.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()


class _DbApiEngine(Engine):
    """Shared behaviour for PEP 249 drivers."""

    def _run(self, sql: str) -> tuple[list[str], list[tuple]]:
        cur = self.raw.cursor()
        try:
            cur.execute(sql)
            return [d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()]
        except Exception as e:  # noqa: BLE001 - any driver error becomes one clear message
            raise ConnectionError_(f"the {self.dialect} query failed: {e}") from None
        finally:
            cur.close()


class PostgresEngine(_DbApiEngine):
    dialect = "postgres"

    def translate(self, sql: str) -> str:
        # sqlglot renames DuckDB's make_date; PostgreSQL has make_date itself.
        return super().translate(sql).replace("DATE_FROM_PARTS(", "MAKE_DATE(")

    def __init__(self, cfg: dict):
        try:
            import psycopg
        except ImportError:
            raise ConnectionError_("PostgreSQL needs psycopg: pip install 'opentabular[postgres]'") from None
        dsn = _setting(cfg, "dsn")
        if not dsn:
            raise ConnectionError_("connection: postgres needs dsn or dsn_env")
        try:
            self.raw = psycopg.connect(dsn, autocommit=True)
        except psycopg.Error as e:
            raise ConnectionError_(f"cannot connect to PostgreSQL: {e}") from None


class SnowflakeEngine(_DbApiEngine):  # not run against a live account
    dialect = "snowflake"

    def __init__(self, cfg: dict):
        try:
            import snowflake.connector
        except ImportError:
            raise ConnectionError_(
                "Snowflake needs its connector: pip install 'opentabular[snowflake]'") from None
        keys = {k[:-4] if k.endswith("_env") else k for k in cfg if k != "type"}
        self.raw = snowflake.connector.connect(**{k: _setting(cfg, k) for k in keys})


class BigQueryEngine(Engine):  # not run against a live project
    dialect = "bigquery"

    def __init__(self, cfg: dict):
        try:
            from google.cloud import bigquery
        except ImportError:
            raise ConnectionError_(
                "BigQuery needs its client: pip install 'opentabular[bigquery]'") from None
        self.raw = bigquery.Client(project=_setting(cfg, "project"))

    def _run(self, sql: str) -> tuple[list[str], list[tuple]]:
        result = self.raw.query(sql).result()
        return [f.name for f in result.schema], [tuple(r.values()) for r in result]


def open_engine(cfg: dict, root: Path, file_sources: dict[str, Path]) -> Engine:
    kind = str(cfg.get("type", "duckdb")).lower()
    if kind == "duckdb":
        return DuckDBEngine(cfg, root, file_sources)
    if file_sources:
        raise ConnectionError_(
            f"file sources ({', '.join(sorted(file_sources))}) only work with DuckDB; "
            f"for {kind}, bind each table with {{table: schema.name}}"
        )
    engines = {"postgres": PostgresEngine, "postgresql": PostgresEngine,
               "snowflake": SnowflakeEngine, "bigquery": BigQueryEngine}
    if kind not in engines:
        raise ConnectionError_(f"connection type '{kind}' is not supported "
                               f"(choose duckdb, postgres, snowflake or bigquery)")
    return engines[kind](cfg)
