"""A project = a TMDL model + a connection + the binding of model tables to
physical relations. Described by an `opentabular.yml` file:

    model: definition            # folder of .tmdl files
    connection:
      type: duckdb
      database: ":memory:"       # or a path to a .duckdb file
    sources:                     # optional, per model table
      Sales: data/sales.csv      # a .csv / .parquet file, exposed as a view
      Product: {table: analytics.dim_product}   # an existing relation

A table with no `sources` entry is read from the relation named by its TMDL
entity partition (`schemaName`.`entityName`), or by the table name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .compiler import CompileError, CompiledQuery, OutputColumn, Query, QueryCompiler, UnsupportedError
from .lineage import measure_lineage, used_by
from .model import Measure, Model, Table
from .security import Identity, SecurityError, row_filters
from .tmdl import load_model

CONFIG_NAME = "opentabular.yml"
_FILE_READERS = {".csv": "read_csv_auto", ".tsv": "read_csv_auto", ".parquet": "read_parquet",
                 ".json": "read_json_auto"}


class ProjectError(Exception):
    pass


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    sql: str
    meta: list[OutputColumn] = field(default_factory=list)  # one per column

    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r)) for r in self.rows]


@dataclass
class Issue:
    level: str  # error | unsupported | warning | info
    where: str
    message: str


@dataclass
class Project:
    root: Path
    model: Model
    config: dict = field(default_factory=dict)
    _con: Any = None

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path = ".") -> "Project":
        path = Path(path)
        if path.is_file() and path.suffix in (".yml", ".yaml"):
            cfg_file, root = path, path.parent
        elif (path / CONFIG_NAME).is_file():
            cfg_file, root = path / CONFIG_NAME, path
        elif path.exists():
            # No config: treat the path itself as the TMDL folder or file.
            root = path if path.is_dir() else path.parent
            return cls(root=root, model=load_model(path))
        else:
            raise ProjectError(f"no such project: {path}")
        config = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise ProjectError(f"{cfg_file}: expected a mapping at the top level")
        model = load_model(root / str(config.get("model", "definition")))
        return cls(root=root, model=model, config=config)

    # -- physical binding ------------------------------------------------
    def _source(self, table: Table) -> dict:
        sources = self.config.get("sources") or {}
        for name, spec in sources.items():
            if str(name).casefold() == table.name.casefold():
                if isinstance(spec, str):
                    is_file = Path(spec).suffix.lower() in _FILE_READERS
                    return {"file": spec} if is_file else {"table": spec}
                if isinstance(spec, dict):
                    return spec
                raise ProjectError(f"sources.{name}: expected a path, a table name or a mapping")
        return {}

    def _view_name(self, table: Table) -> str:
        return table.entity_name or table.name

    def relation_for(self, table: Table) -> str:
        q = lambda s: '"' + s.replace('"', '""') + '"'  # noqa: E731
        src = self._source(table)
        if "file" in src:
            return q(self._view_name(table))
        if "table" in src:
            return ".".join(q(p) for p in str(src["table"]).split("."))
        name = q(table.entity_name or table.name)
        return f"{q(table.schema_name)}.{name}" if table.schema_name else name

    # -- connection ------------------------------------------------------
    def connect(self):
        if self._con is not None:
            return self._con
        conn_cfg = self.config.get("connection") or {}
        kind = str(conn_cfg.get("type", "duckdb")).lower()
        if kind != "duckdb":
            raise ProjectError(f"connection type '{kind}' is not supported yet (only duckdb)")
        import duckdb

        database = str(conn_cfg.get("database", ":memory:"))
        if database != ":memory:":
            database = str((self.root / database).resolve())
        con = duckdb.connect(database)
        for table in self.model.tables.values():
            src = self._source(table)
            if "file" not in src:
                continue
            file = (self.root / str(src["file"])).resolve()
            reader = _FILE_READERS.get(file.suffix.lower())
            if reader is None:
                raise ProjectError(f"sources.{table.name}: unsupported file type {file.suffix}")
            if not file.exists():
                raise ProjectError(f"sources.{table.name}: file not found: {file}")
            path_sql = "'" + str(file).replace("'", "''") + "'"
            view = '"' + self._view_name(table).replace('"', '""') + '"'
            con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM {reader}({path_sql})")
        self._con = con
        return con

    # -- queries ---------------------------------------------------------
    def compiler(self, allow_adhoc_measures: bool = False, identity: Identity | None = None) -> QueryCompiler:
        """`identity` selects the row-level security to apply; None means no
        restriction (a developer working on the model)."""
        try:
            filters = row_filters(self.model, identity)
        except SecurityError as e:
            raise CompileError(str(e)) from None
        return QueryCompiler(
            self.model, self.relation_for, allow_adhoc_measures=allow_adhoc_measures,
            row_filters=filters, user=identity.name if identity else None,
        )

    def compile(self, query: Query, allow_adhoc_measures: bool = False,
                identity: Identity | None = None) -> CompiledQuery:
        return self.compiler(allow_adhoc_measures, identity).compile(query)

    def run(self, query: Query, allow_adhoc_measures: bool = False,
            identity: Identity | None = None) -> Result:
        compiled = self.compile(query, allow_adhoc_measures, identity)
        cur = self.connect().execute(compiled.sql)
        return Result([c.name for c in compiled.columns], cur.fetchall(), compiled.sql, compiled.columns)

    def run_dax(self, text: str, allow_adhoc_measures: bool = False,
                identity: Identity | None = None) -> Result:
        """Run a DAX query (EVALUATE ...). Columns are named as Power BI names
        them: Table[Column] for columns, [Name] for measures."""
        from .dax import ast as A
        from .dax.query import DaxQueryError, parse_dax_query

        try:
            dq = parse_dax_query(text)
        except DaxQueryError as e:
            raise CompileError(str(e)) from None
        group_by = [f"'{c.table}'[{c.column}]" for c in dq.group_by]
        query = Query(measures=list(dq.measures), group_by=group_by, filters=list(dq.filters),
                      limit=dq.limit, defines=list(dq.defines))
        compiler = self.compiler(allow_adhoc_measures, identity)
        probe = compiler.compile(Query(query.measures, group_by, query.filters, defines=query.defines))
        by_source = {(c.source or "").casefold(): c.name for c in probe.columns if c.kind == "dimension"}
        by_measure = {c.name.casefold(): c.name for c in probe.columns if c.kind == "measure"}
        for expr, desc in dq.order_by:
            name = None
            if isinstance(expr, A.ColumnRef):
                col = self.model.table(expr.table).column(expr.column) if self.model.has_table(expr.table) else None
                if col is not None:
                    name = by_source.get(f"{col.table}[{col.name}]".casefold())
                else:
                    name = by_measure.get(expr.column.casefold())
            elif isinstance(expr, A.BareRef):
                name = by_measure.get(expr.name.casefold())
            if name is None:
                raise CompileError("ORDER BY and TOPN can only order by a column or measure of the result")
            query.order_by.append(name + (" desc" if desc else ""))
        compiled = compiler.compile(query)
        cur = self.connect().execute(compiled.sql)
        names = [f"[{c.name}]" if c.kind == "measure" else str(c.source) for c in compiled.columns]
        return Result(names, cur.fetchall(), compiled.sql, compiled.columns)

    # -- validation ------------------------------------------------------
    def validate(self, check_sources: bool = False) -> list[Issue]:
        """Static checks; with `check_sources`, also verify that every bound
        column exists in the database."""
        issues = [Issue("error", "model", m) for m in self.model.structural_issues()]
        issues += [
            Issue("warning", "model", f"{u} is present in the TMDL but not used by the engine yet")
            for u in self.model.unsupported
        ]
        compiler = self.compiler()
        for role in self.model.roles.values():
            for table, dax in role.table_filters.items():
                try:
                    QueryCompiler(self.model, self.relation_for, row_filters={table: dax}, user="someone")
                except (UnsupportedError, CompileError) as e:
                    level = "unsupported" if isinstance(e, UnsupportedError) else "error"
                    issues.append(Issue(level, f"role '{role.name}'", str(e)))
                except Exception as e:  # noqa: BLE001 - unknown table and the like
                    issues.append(Issue("error", f"role '{role.name}'", str(e)))
        for table in self.model.tables.values():
            for col in table.columns.values():
                if col.expression is None:
                    continue
                where = f"'{table.name}'[{col.name}]"
                try:
                    compiler._calc_ast(col)
                except UnsupportedError as e:
                    issues.append(Issue("unsupported", where, str(e)))
                except CompileError as e:
                    issues.append(Issue("error", where, str(e)))
            for m in table.measures.values():
                where = f"[{m.name}]"
                try:
                    compiler.check_measure(m)
                except UnsupportedError as e:
                    issues.append(Issue("unsupported", where, _strip_prefix(str(e), m.name)))
                except CompileError as e:
                    issues.append(Issue("error", where, _strip_prefix(str(e), m.name)))
                if not m.description and not m.is_hidden:
                    issues.append(Issue(
                        "info", where, "no description (/// comment); AI clients rely on descriptions"
                    ))
        if check_sources:
            con = self.connect()
            for table in self.model.tables.values():
                cols = [c for c in table.columns.values() if c.expression is None]
                if not cols:
                    continue
                rel = self.relation_for(table)
                try:
                    have = {d[0].casefold() for d in con.execute(f"SELECT * FROM {rel} LIMIT 0").description}
                except Exception as e:  # noqa: BLE001 - surface any driver error
                    issues.append(Issue("error", f"'{table.name}'", f"cannot read {rel}: {e}"))
                    continue
                for c in cols:
                    if c.physical_name.casefold() not in have:
                        issues.append(Issue(
                            "error", f"'{table.name}'[{c.name}]",
                            f"source column {c.physical_name!r} not found in {rel}",
                        ))
        return issues

    # -- catalogue -------------------------------------------------------
    def describe(self, include_hidden: bool = False, include_dax: bool = True) -> dict:
        """A JSON-serialisable description of the model, meant for people and
        for AI clients choosing measures and dimensions."""
        tables = []
        for t in self.model.tables.values():
            if t.is_hidden and not include_hidden:
                continue
            tables.append({
                "name": t.name,
                "description": t.description,
                "columns": [
                    {"ref": f"'{t.name}'[{c.name}]", "name": c.name, "type": c.data_type,
                     "description": c.description}
                    for c in t.columns.values()
                    if include_hidden or not c.is_hidden
                ],
            })
        measures = []
        for m in self.model.all_measures():
            if m.is_hidden and not include_hidden:
                continue
            entry = {"name": m.name, "table": m.table, "description": m.description,
                     "format": m.format_string, "folder": m.display_folder,
                     "owner": m.owner, "trust": m.trust}
            if include_dax:
                entry["dax"] = m.expression
            measures.append(entry)
        rels = [
            {"from": f"'{r.from_table}'[{r.from_column}]", "to": f"'{r.to_table}'[{r.to_column}]",
             "active": r.is_active}
            for r in self.model.relationships
        ]
        return {"model": self.model.name, "tables": tables, "measures": measures, "relationships": rels,
                "roles": [r.name for r in self.model.roles.values()]}


    # -- lineage and search ----------------------------------------------
    def lineage(self, measure: Measure) -> dict:
        """Where a measure's numbers come from: the measures and columns it is
        built on, the physical relations behind them, and what depends on it."""
        info = measure_lineage(self.model, measure)
        sources = []
        for name in info["tables"]:
            t = self.model.table(name)
            cols = [c for c in info["columns"] if c.startswith(f"'{t.name}'[")]
            sources.append({
                "table": t.name,
                "relation": self.relation_for(t).replace('"', ""),
                "columns": {
                    c: self.model.column(t.name, c[len(t.name) + 3:-1]).physical_name for c in cols
                },
            })
        info["sources"] = sources
        info["used_by"] = used_by(self.model, measure)
        return info

    def explain(self, name: str) -> dict | None:
        m = self.model.find_measure(name.strip().strip("[]"))
        if m is None:
            return None
        return {"name": m.name, "table": m.table, "description": m.description,
                "format": m.format_string, "folder": m.display_folder, "owner": m.owner,
                "trust": m.trust, "dax": m.expression, "lineage": self.lineage(m)}

    def search(self, text: str, limit: int = 20) -> list[dict]:
        """Find measures and columns by words in their name, description or
        folder. Every word must match somewhere; name matches rank first."""
        words = [w for w in text.casefold().split() if w]
        if not words:
            return []
        hits: list[tuple[int, dict]] = []

        def score(name: str, *others: str | None) -> int:
            name_l = name.casefold()
            rest = " ".join(o.casefold() for o in others if o)
            total = 0
            for w in words:
                if w in name_l:
                    total += 3 if name_l == w or name_l.startswith(w) else 2
                elif w in rest:
                    total += 1
                else:
                    return 0
            return total

        for m in self.model.all_measures():
            if m.is_hidden:
                continue
            s = score(m.name, m.description, m.display_folder)
            if s:
                hits.append((s, {"kind": "measure", "name": m.name, "description": m.description,
                                 "trust": m.trust}))
        for t in self.model.tables.values():
            if t.is_hidden:
                continue
            for c in t.columns.values():
                if c.is_hidden:
                    continue
                s = score(c.name, c.description, t.name)
                if s:
                    hits.append((s, {"kind": "column", "name": f"'{t.name}'[{c.name}]",
                                     "description": c.description}))
        hits.sort(key=lambda h: (-h[0], h[1]["kind"] != "measure", h[1]["name"]))
        return [h for _, h in hits[:limit]]


def _strip_prefix(message: str, measure: str) -> str:
    prefix = f"in measure [{measure}]: "
    return message[len(prefix):] if message.startswith(prefix) else message
