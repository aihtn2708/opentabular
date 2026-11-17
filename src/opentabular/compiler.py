"""Compile a semantic query (measures + group-by columns + filters) to SQL.

How DAX evaluation is mapped onto SQL
-------------------------------------
A semantic query behaves like DAX `SUMMARIZECOLUMNS`: the group-by columns and
the query filters form the *filter context*, and every measure is evaluated in
that context.

1. Each measure expression is walked while tracking the filter context.
   `CALCULATE` produces a modified context (filters added, replaced or removed,
   time intelligence applied) for the expression inside it.
2. Every aggregation (`SUM`, `SUMX`, `COUNTROWS`, ...) met during the walk
   becomes a *leaf*: one aggregate over one base table under one context.
   Identical leaves are shared between measures.
3. Each leaf compiles to its own `SELECT ... GROUP BY` over the base table
   joined to the dimensions it needs (many-to-one joins only, so no fan-out).
   A leaf is grouped only by the group-by columns that are still active in its
   context and that can filter its base table.
4. A *spine* of group-by combinations is built, every leaf is joined to it on
   the keys they share, and the measure arithmetic is applied on top. Rows
   where every measure is blank are dropped, as SUMMARIZECOLUMNS does.

Because a leaf that removed a filter (for example `ALL(Product)`) simply has
fewer keys, "percent of total" style measures fall out of the join naturally.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable

from .dax import ast as A
from .dax import DaxSyntaxError, parse_dax
from .model import Column, Measure, Model, ModelError, Table


class CompileError(Exception):
    """The query or a measure is invalid."""


class UnsupportedError(CompileError):
    """Valid DAX that this compiler does not translate yet."""


# ---------------------------------------------------------------------------
# Dialect
# ---------------------------------------------------------------------------

class DuckDBDialect:
    name = "duckdb"

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def string(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def null_safe_eq(self, a: str, b: str) -> str:
        return f"{a} IS NOT DISTINCT FROM {b}"

    def to_date(self, expr: str) -> str:
        return f"CAST({expr} AS DATE)"

    def date_add(self, expr: str, n: int, unit: str) -> str:
        if unit == "QUARTER":
            n, unit = n * 3, "MONTH"
        if n == 0:
            return expr
        sign = "+" if n > 0 else "-"
        return f"CAST({expr} {sign} INTERVAL {abs(n)} {unit} AS DATE)"

    def date_trunc(self, unit: str, expr: str) -> str:
        return f"CAST(date_trunc('{unit.lower()}', {expr}) AS DATE)"

    def make_date(self, y: str, m: str, d: str) -> str:
        return f"make_date(CAST({y} AS INTEGER), CAST({m} AS INTEGER), CAST({d} AS INTEGER))"


# ---------------------------------------------------------------------------
# Query and context data structures
# ---------------------------------------------------------------------------

@dataclass
class Query:
    """A semantic query.

    measures  names of model measures (or "Name = <DAX>" when ad-hoc measures
              are allowed)
    group_by  columns as "Table[Column]"
    filters   DAX predicates over one column, e.g. "Product[Color] = \"Red\"",
              or dicts {"column": "Product[Color]", "op": "=", "value": "Red"}
    order_by  output column names, optionally followed by " desc"
    """

    measures: list[object] = field(default_factory=list)  # names, or (name, parsed DAX)
    group_by: list[str] = field(default_factory=list)
    filters: list[object] = field(default_factory=list)  # DAX text, dicts, or parsed DAX
    order_by: list[str] = field(default_factory=list)
    limit: int | None = None
    # Measures defined by the query itself (DEFINE MEASURE): (table, name, parsed DAX).
    defines: list[tuple[str, str, object]] = field(default_factory=list)


@dataclass
class OutputColumn:
    name: str
    kind: str  # "dimension" | "measure"
    format_string: str | None = None
    source: str | None = None  # Table[Column] or measure name
    # Measures only. certified / governed: defined in the model.
    # ad hoc: written in the query, so not reviewed with the model.
    trust: str | None = None
    owner: str | None = None


@dataclass
class CompiledQuery:
    sql: str
    columns: list[OutputColumn]


ColKey = tuple[str, str]  # (table name, column name), casefolded


@dataclass(frozen=True)
class Filter:
    columns: frozenset[ColKey]
    expr: A.Node  # row-level predicate with canonical column references

    @property
    def tables(self) -> frozenset[str]:
        return frozenset(t for t, _ in self.columns)


@dataclass(frozen=True)
class TimeOp:
    kind: str  # "shift" | "todate" | "window"
    n: int = 0
    unit: str = "YEAR"  # YEAR | QUARTER | MONTH | DAY | ALL


@dataclass(frozen=True)
class Ctx:
    """A filter context: which group-by columns still apply, plus filters."""

    group: tuple[int, ...] = ()
    filters: tuple[Filter, ...] = ()
    time_col: ColKey | None = None
    time_ops: tuple[TimeOp, ...] = ()
    # True while iterating the values of a column (see `_nested`): the
    # iterated columns are readable but do not filter until a measure
    # reference or CALCULATE performs the context transition.
    rowctx: bool = False
    # Names of inactive relationships switched on by USERELATIONSHIP.
    userel: frozenset[str] = frozenset()


@dataclass(frozen=True)
class NestedLeaf:
    """An aggregate over the rows of an inner query: `SUMX(VALUES(col), [M])`."""

    agg: str
    sql: str  # inner query: columns g0..gN (outer keys first), then v (and o)
    keys: tuple[int, ...]  # outer group-by indexes of the first len(keys) columns
    top: int | None = None  # TOPN: keep the best `top` rows per outer key, by column o
    top_asc: bool = False


@dataclass(frozen=True)
class TableLeaf:
    """An inner query kept as a table (not aggregated, not joined): RANKX
    compares the current value against its rows."""

    sql: str
    keys: tuple[int, ...]


@dataclass(frozen=True)
class Leaf:
    agg: str  # SUM AVG MIN MAX COUNT DCOUNT DCOUNT_NB COUNTROWS
    base: str  # casefolded table name
    expr: A.Node | None
    row_filters: tuple[A.Node, ...]
    ctx: Ctx


@dataclass
class _Var:
    sql: str
    ast: A.Node | None  # set when the variable is a constant expression


_SIMPLE_AGG = {
    "SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX",
    "COUNT": "COUNT", "COUNTA": "COUNT",
    "DISTINCTCOUNT": "DCOUNT", "DISTINCTCOUNTNOBLANK": "DCOUNT_NB",
}
_ITER_AGG = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX", "COUNTX": "COUNT"}
_AGG_FUNCS = set(_SIMPLE_AGG) | set(_ITER_AGG) | {
    "COUNTROWS", "CALCULATE", "TOTALYTD", "TOTALQTD", "TOTALMTD", "RANKX", "SELECTEDVALUE", "HASONEVALUE",
}
_PERIOD_FUNCS = {
    "PREVIOUSDAY": ("prev", "DAY"), "PREVIOUSMONTH": ("prev", "MONTH"),
    "PREVIOUSQUARTER": ("prev", "QUARTER"), "PREVIOUSYEAR": ("prev", "YEAR"),
    "NEXTDAY": ("next", "DAY"), "NEXTMONTH": ("next", "MONTH"),
    "NEXTQUARTER": ("next", "QUARTER"), "NEXTYEAR": ("next", "YEAR"),
}
_TIME_FUNCS = {
    "SAMEPERIODLASTYEAR", "DATEADD", "DATESYTD", "DATESQTD", "DATESMTD", "DATESINPERIOD",
    "PARALLELPERIOD",
} | set(_PERIOD_FUNCS)
_UNITS = {"YEAR", "QUARTER", "MONTH", "DAY"}
_KNOWN_UNSUPPORTED = {
    "CROSSFILTER", "ALLSELECTED", "TREATAS",
    "VALUES", "DISTINCT", "SUMMARIZE", "SUMMARIZECOLUMNS", "ADDCOLUMNS", "TOPN",
    "EARLIER", "LOOKUPVALUE", "CONCATENATEX",
    "FIRSTDATE", "LASTDATE", "CALCULATETABLE", "GENERATE", "CROSSJOIN",
}

_PURE_FUNCS = {
    "IF", "SWITCH", "DIVIDE", "COALESCE", "ISBLANK", "BLANK", "TRUE", "FALSE", "AND", "OR",
    "NOT", "MIN", "MAX", "ABS", "ROUND", "INT", "SQRT", "POWER", "MOD", "EXP", "LN", "YEAR",
    "QUARTER", "MONTH", "DAY", "DATE", "UPPER", "LOWER", "LEN", "LEFT", "RIGHT", "TRIM",
    "CONCATENATE", "CONTAINSSTRING", "USERNAME", "USERPRINCIPALNAME",
}

# Every function the compiler accepts, exposed for documentation and tooling.
SUPPORTED_FUNCTIONS = sorted(
    _AGG_FUNCS | _TIME_FUNCS | _PURE_FUNCS
    | {"ALL", "REMOVEFILTERS", "ALLEXCEPT", "KEEPFILTERS", "FILTER", "RELATED",
       "VALUES", "DISTINCT", "SUMMARIZE", "TOPN", "USERELATIONSHIP", "DATESBETWEEN",
       "ISINSCOPE", "ISFILTERED"}
)


def _k(name: str) -> str:
    return name.casefold()


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

class QueryCompiler:
    def __init__(
        self,
        model: Model,
        relation_for: Callable[[Table], str] | None = None,
        dialect: DuckDBDialect | None = None,
        allow_adhoc_measures: bool = False,
        row_filters: dict[str, str] | None = None,
        user: str | None = None,
    ):
        """`row_filters` maps a table name to a DAX condition that every query
        must respect (row-level security); `user` is what USERNAME() returns."""
        self.model = model
        self.d = dialect or DuckDBDialect()
        self.relation_for = relation_for or self._default_relation
        self.allow_adhoc = allow_adhoc_measures
        self._parsed: dict[str, A.Node] = {}
        self.row_cols: frozenset[int] = frozenset()  # set only in nested compilers
        self.user = user
        self.defined: dict[str, Measure] = {}  # measures defined by the query itself
        self._calc: dict[ColKey, A.Node] = {}
        self._calc_stack: list[ColKey] = []
        self.security: dict[str, A.Node] = {}
        for table, dax in (row_filters or {}).items():
            t = self.model.table(table)
            try:
                node = self._rowify(self._parse(dax, f"row filter on '{t.name}'"), _k(t.name), {}, {})
            except CompileError as e:
                raise type(e)(f"row filter on '{t.name}': {e}") from None
            self.security[_k(t.name)] = node

    def _child(self) -> "QueryCompiler":
        """A compiler for an inner query, sharing this one's configuration."""
        sub = QueryCompiler(self.model, self.relation_for, self.d, self.allow_adhoc)
        sub._parsed, sub._calc = self._parsed, self._calc
        sub.security, sub.user, sub.defined = self.security, self.user, self.defined
        sub.leaves = {}
        return sub

    def _find_measure(self, name: str) -> Measure | None:
        return self.defined.get(_k(name)) or self.model.find_measure(name)

    def _default_relation(self, table: Table) -> str:
        name = self.d.quote(table.entity_name or table.name)
        if table.schema_name:
            return f"{self.d.quote(table.schema_name)}.{name}"
        return name

    # -- public ----------------------------------------------------------
    def compile(self, query: Query) -> CompiledQuery:
        self.leaves: dict[Leaf | NestedLeaf | TableLeaf, int] = {}
        self.row_cols = frozenset()
        self.group_cols: list[Column] = [self._parse_group_col(g) for g in query.group_by]
        if len({(_k(c.table), _k(c.name)) for c in self.group_cols}) != len(self.group_cols):
            raise CompileError("group_by lists the same column twice")
        self.defined = {}
        if query.defines and not self.allow_adhoc:
            raise CompileError("DEFINE MEASURE is an ad-hoc measure (ad-hoc measures are disabled for this interface)")
        for table, name, node in query.defines:
            self.defined[_k(name)] = Measure(name, table, "(defined in the query)")
            self._parsed[_k(name)] = node  # type: ignore[assignment]
        plain = [f for f in query.filters if not isinstance(f, A.Node)]
        self.query_filters = tuple(self._parse_query_filter(f) for f in plain)
        if not query.measures and not self.group_cols:
            raise CompileError("a query needs at least one measure or one group_by column")

        base = Ctx(group=tuple(range(len(self.group_cols))), filters=self.query_filters)
        parsed = tuple(A.Call("KEEPFILTERS", (f,)) for f in query.filters if isinstance(f, A.Node))
        if parsed:  # filters of a DAX query restrict the result; they never replace a grouping
            base = self._apply_filters(base, parsed, {})
            self.query_filters = base.filters
        measures: list[tuple[str, str, Measure | None]] = []  # (name, sql, model measure)
        for spec in query.measures:
            name, node, m = self._resolve_measure_spec(spec)
            if m is not None:
                stack: tuple[str, ...] = (_k(m.name),)
            else:  # a DAX query may name an output after the measure it shows
                stack = () if isinstance(spec, tuple) else (_k(name),)
            try:
                sql = self._scalar(node, base, {}, stack)
            except CompileError as e:
                raise type(e)(f"in measure [{name}]: {e}") from None
            measures.append((name, sql, m))
        if len({_k(m[0]) for m in measures}) != len(measures):
            raise CompileError("the same measure name is requested twice")
        return self._assemble(query, measures)

    def check_measure(self, measure: Measure) -> None:
        """Dry-compile one measure with no grouping; raises on any problem."""
        self.compile(Query(measures=[measure.name]))

    # -- query parsing ---------------------------------------------------
    def _parse(self, text: str, what: str) -> A.Node:
        try:
            return parse_dax(text)
        except DaxSyntaxError as e:
            raise CompileError(f"{what}: {e}") from None

    def _column(self, table: str, column: str) -> Column:
        try:
            col = self.model.column(table, column)
        except ModelError as e:
            raise CompileError(str(e)) from None
        return col

    def _calc_ast(self, col: Column) -> A.Node:
        """The row-level expression of a calculated column, in canonical form."""
        key = (_k(col.table), _k(col.name))
        if key not in self._calc:
            if key in self._calc_stack:
                raise CompileError(f"calculated column '{col.table}'[{col.name}] refers to itself")
            self._calc_stack.append(key)
            try:
                node = self._parse(col.expression or "", f"calculated column '{col.table}'[{col.name}]")
                self._calc[key] = self._rowify(node, _k(col.table), {}, {})
                self._expr_tables(self._calc[key])  # also checks nested calculated columns
            except CompileError as e:
                raise type(e)(f"in calculated column '{col.table}'[{col.name}]: {e}") from None
            finally:
                self._calc_stack.pop()
        return self._calc[key]

    def _expr_tables(self, node: A.Node | None) -> set[str]:
        """Tables a row-level expression reads, through calculated columns too."""
        out: set[str] = set()
        if node is None:
            return out
        for n in A.walk(node):
            if isinstance(n, A.ColumnRef):
                out.add(_k(n.table))
                col = self.model.column(n.table, n.column)
                if col.expression is not None:
                    out |= self._expr_tables(self._calc_ast(col))
        return out

    def _colsql(self, table: str, column: str) -> str:
        col = self.model.column(table, column)
        return f"{self.d.quote(self.model.table(table).name)}.{self.d.quote(col.physical_name)}"

    def _parse_group_col(self, spec: str) -> Column:
        node = self._parse(spec, f"group_by {spec!r}")
        if not isinstance(node, A.ColumnRef):
            raise CompileError(f"group_by {spec!r}: expected Table[Column]")
        return self._column(node.table, node.column)

    def _parse_query_filter(self, spec: object) -> Filter:
        if isinstance(spec, dict):
            node = self._filter_from_dict(spec)
        else:
            node = self._parse(str(spec), f"filter {spec!r}")
        flt = self._bool_filter(node, {})
        if flt is None:
            raise CompileError(f"filter {spec!r}: expected a condition on a column")
        if len(flt.tables) != 1:
            raise CompileError(f"filter {spec!r}: a query filter may reference one table only")
        return flt

    def _filter_from_dict(self, spec: dict) -> A.Node:
        try:
            col = self._parse(str(spec["column"]), "filter column")
            op = str(spec.get("op", "=")).strip().lower()
            value = spec["value"]
        except KeyError as e:
            raise CompileError(f"filter is missing key {e}") from None
        if not isinstance(col, A.ColumnRef):
            raise CompileError(f"filter column {spec['column']!r}: expected Table[Column]")

        def lit(v) -> A.Node:
            if isinstance(v, bool):
                return A.Bool(v)
            if isinstance(v, (int, float)):
                return A.Num(repr(v)) if v >= 0 else A.Unary("-", A.Num(repr(-v)))
            return A.Str(str(v))

        if op in ("in", "not in", "not_in"):
            if not isinstance(value, (list, tuple)):
                raise CompileError("filter with op 'in' needs a list value")
            node: A.Node = A.Binary("IN", col, A.SetLiteral(tuple(lit(v) for v in value)))
            return node if op == "in" else A.Unary("NOT", node)
        op = {"!=": "<>", "==": "="}.get(op, op)
        if op not in ("=", "<>", "<", "<=", ">", ">="):
            raise CompileError(f"unknown filter op {op!r}")
        return A.Binary(op, col, lit(value))

    def _resolve_measure_spec(self, spec: object) -> tuple[str, A.Node, Measure | None]:
        if isinstance(spec, tuple):  # (output name, parsed DAX), from a DAX query
            name, node = spec
            target = None
            if isinstance(node, A.BareRef):
                target = self._find_measure(node.name)
            elif isinstance(node, A.ColumnRef) and self.model.has_table(node.table) \
                    and self.model.table(node.table).column(node.column) is None:
                target = self._find_measure(node.column)
            if target is not None and _k(target.name) not in self.defined:
                return name, self._measure_ast(target), target  # a model measure: governed
            if not self.allow_adhoc:
                raise CompileError(
                    f'"{name}" is an ad-hoc expression (ad-hoc measures are disabled for this interface); '
                    "ask for a model measure, as in \"Name\", [Measure]"
                )
            return name, node, None
        text = str(spec).strip()
        name = text[1:-1] if text.startswith("[") and text.endswith("]") else text
        m = self._find_measure(name)
        if m is not None:
            return m.name, self._measure_ast(m), m
        if "=" in text:
            if not self.allow_adhoc:
                raise CompileError(
                    f"unknown measure {spec!r} (ad-hoc measures are disabled for this interface)"
                )
            name, _, dax = text.partition("=")
            name = name.strip().strip("[]")
            if not name:
                raise CompileError(f"ad-hoc measure {spec!r} needs a name: 'Name = <DAX>'")
            return name, self._parse(dax, f"measure [{name}]"), None
        raise CompileError(f"unknown measure {spec!r}")

    def _measure_ast(self, m: Measure) -> A.Node:
        key = _k(m.name)
        if key not in self._parsed:
            self._parsed[key] = self._parse(m.expression, f"measure [{m.name}]")
        return self._parsed[key]

    # -- name resolution -------------------------------------------------
    def _table_of(self, node: A.Node) -> Table | None:
        if isinstance(node, (A.NameRef, A.TableRef)) and self.model.has_table(node.name):
            return self.model.table(node.name)
        return None

    def _resolve_colref(self, node: A.ColumnRef) -> Column | Measure:
        if not self.model.has_table(node.table):
            raise CompileError(f"unknown table '{node.table}'")
        col = self.model.table(node.table).column(node.column)
        if col is not None:
            return self._column(node.table, node.column)
        m = self._find_measure(node.column)
        if m is not None:
            return m
        raise CompileError(f"unknown column '{node.table}'[{node.column}]")

    # -- measure-level expressions ---------------------------------------
    def _scalar(self, node: A.Node, ctx: Ctx, env: dict[str, _Var], stack: tuple[str, ...]) -> str:
        rec = lambda n: self._scalar(n, ctx, env, stack)  # noqa: E731

        if isinstance(node, A.BareRef):
            m = self._find_measure(node.name)
            if m is None:
                raise CompileError(
                    f"[{node.name}] is not a measure, and a column cannot be used here without an aggregation"
                )
            return self._measure_ref(m, ctx, stack)
        if isinstance(node, A.ColumnRef):
            target = self._resolve_colref(node)
            if isinstance(target, Measure):
                return self._measure_ref(target, ctx, stack)
            if ctx.rowctx:
                key = (_k(target.table), _k(target.name))
                for j in self.row_cols:
                    c = self.group_cols[j]
                    if (_k(c.table), _k(c.name)) == key:
                        return f'"s"."g{j}"'
            raise CompileError(
                f"'{target.table}'[{target.name}] is used without an aggregation such as SUM"
            )
        if isinstance(node, A.NameRef):
            var = env.get(_k(node.name))
            if var is None:
                raise CompileError(f"unknown name '{node.name}'")
            return f"({var.sql})"
        if isinstance(node, A.TableRef):
            raise CompileError(f"table '{node.name}' cannot be used as a value")
        if isinstance(node, A.VarBlock):
            env2 = dict(env)
            for name, expr in node.vars:
                sql = self._scalar(expr, ctx, env2, stack)
                const = self._inline_consts(expr, env2) if self._const_with(expr, env2) else None
                env2[_k(name)] = _Var(sql, const)
            return self._scalar(node.body, ctx, env2, stack)
        if isinstance(node, A.Call):
            name = node.name
            if name in _SIMPLE_AGG and not (name in ("MIN", "MAX") and len(node.args) == 2):
                return self._simple_agg(name, node, ctx)
            if name in _ITER_AGG:
                self._arity(node, 2)
                plan = self._iter_plan(node.args[0], ctx, node.args[1])
                if plan is not None:
                    return self._nested(_ITER_AGG[name], plan, env, stack)
                base, row_filters, ctx2 = self._table_arg(node.args[0], ctx, env)
                expr = self._rowify(node.args[1], base, env, {})
                return self._leaf(_ITER_AGG[name], base, expr, row_filters, ctx2)
            if name == "COUNTROWS":
                self._arity(node, 1)
                arg = node.args[0]
                if isinstance(arg, A.Call) and arg.name in ("VALUES", "DISTINCT") and len(arg.args) == 1 \
                        and isinstance(arg.args[0], A.ColumnRef):
                    return self._simple_agg("DISTINCTCOUNT", A.Call("DISTINCTCOUNT", arg.args), ctx)
                plan = self._iter_plan(arg, ctx, A.Num("1"))
                if plan is not None:
                    return self._nested("SUM", plan, env, stack)
                base, row_filters, ctx2 = self._table_arg(arg, ctx, env)
                return self._leaf("COUNTROWS", base, None, row_filters, ctx2)
            if name == "CALCULATE":
                if not node.args:
                    raise CompileError("CALCULATE needs an expression")
                new_ctx = self._apply_filters(self._transition(ctx), node.args[1:], env)
                return self._scalar(node.args[0], new_ctx, env, stack)
            if name in ("TOTALYTD", "TOTALQTD", "TOTALMTD"):
                if len(node.args) not in (2, 3):
                    raise CompileError(f"{name} takes 2 or 3 arguments")
                dates = A.Call("DATES" + name[5:], (node.args[1],))
                new_ctx = self._apply_filters(self._transition(ctx), (dates,) + tuple(node.args[2:]), env)
                return self._scalar(node.args[0], new_ctx, env, stack)
            if name == "RELATED":
                if ctx.rowctx and len(node.args) == 1:
                    return rec(node.args[0])
                raise CompileError("RELATED needs a row context (use it inside SUMX, FILTER, ...)")
            if name in ("SELECTEDVALUE", "HASONEVALUE"):
                self._arity(node, *((1, 2) if name == "SELECTEDVALUE" else (1,)))
                col = self._resolve_colref(node.args[0]) if isinstance(node.args[0], A.ColumnRef) else None
                if not isinstance(col, Column):
                    raise CompileError(f"{name} expects a column reference such as Table[Column]")
                ref = A.ColumnRef(col.table, col.name)
                if name == "HASONEVALUE":
                    return f"COALESCE({self._leaf('HASONE', _k(col.table), ref, (), ctx)}, FALSE)"
                value = self._leaf("ONEVALUE", _k(col.table), ref, (), ctx)
                return f"COALESCE({value}, {rec(node.args[1])})" if len(node.args) == 2 else value
            if name in ("ISINSCOPE", "ISFILTERED"):
                self._arity(node, 1)
                col = self._resolve_colref(node.args[0]) if isinstance(node.args[0], A.ColumnRef) else None
                if not isinstance(col, Column):
                    raise CompileError(f"{name} expects a column reference such as Table[Column]")
                key = (_k(col.table), _k(col.name))
                grouped = any(
                    (_k(self.group_cols[i].table), _k(self.group_cols[i].name)) == key for i in ctx.group
                )
                filtered = grouped or (name == "ISFILTERED" and any(key in f.columns for f in ctx.filters))
                return "TRUE" if filtered else "FALSE"
            if name == "RANKX":
                return self._rankx(node, ctx, env, stack)
        return self._pure(node, rec)

    def _measure_ref(self, m: Measure, ctx: Ctx, stack: tuple[str, ...]) -> str:
        key = _k(m.name)
        if key in stack:
            raise CompileError(f"circular reference: measure [{m.name}] refers to itself")
        try:
            return self._scalar(self._measure_ast(m), self._transition(ctx), {}, stack + (key,))
        except CompileError as e:
            if str(e).startswith("in measure ["):
                raise
            raise type(e)(f"in measure [{m.name}]: {e}") from None

    def _const_with(self, node: A.Node, env: dict[str, _Var]) -> bool:
        for n in A.walk(node):
            if isinstance(n, A.NameRef):
                var = env.get(_k(n.name))
                if var is None or var.ast is None:
                    return False
            elif isinstance(n, (A.ColumnRef, A.BareRef, A.TableRef, A.VarBlock)):
                return False
            elif isinstance(n, A.Call) and n.name in _AGG_FUNCS | _TIME_FUNCS | {"RELATED"}:
                return False
        return True

    def _inline_consts(self, node: A.Node, env: dict[str, _Var]) -> A.Node:
        if isinstance(node, A.NameRef):
            return env[_k(node.name)].ast  # type: ignore[return-value]
        if isinstance(node, A.Call):
            return A.Call(node.name, tuple(self._inline_consts(a, env) for a in node.args))
        if isinstance(node, A.Unary):
            return A.Unary(node.op, self._inline_consts(node.operand, env))
        if isinstance(node, A.Binary):
            return A.Binary(node.op, self._inline_consts(node.left, env), self._inline_consts(node.right, env))
        if isinstance(node, A.SetLiteral):
            return A.SetLiteral(tuple(self._inline_consts(a, env) for a in node.items))
        return node

    @staticmethod
    def _arity(node: A.Call, *allowed: int) -> None:
        if len(node.args) not in allowed:
            want = " or ".join(str(a) for a in allowed)
            raise CompileError(f"{node.name} takes {want} argument(s), got {len(node.args)}")

    def _simple_agg(self, name: str, node: A.Call, ctx: Ctx) -> str:
        self._arity(node, 1)
        arg = node.args[0]
        if not isinstance(arg, A.ColumnRef):
            raise CompileError(f"{name} expects a column reference such as Table[Column]")
        col = self._resolve_colref(arg)
        if isinstance(col, Measure):
            raise CompileError(f"{name} expects a column, but [{col.name}] is a measure")
        return self._leaf(_SIMPLE_AGG[name], _k(col.table), A.ColumnRef(col.table, col.name), (), ctx)

    # -- iterating the values of columns (level of detail) ---------------
    def _transition(self, ctx: Ctx) -> Ctx:
        """Context transition: the iterated columns start to filter."""
        if not ctx.rowctx:
            return ctx
        return replace(ctx, group=tuple(sorted(set(ctx.group) | self.row_cols)), rowctx=False)

    def _column_iter(self, node: A.Node, ctx: Ctx) -> tuple[list[Column], A.Node | None, Ctx] | None:
        """Recognise an iterator source made of column values:
        VALUES(col), DISTINCT(col), ALL(col, ...), SUMMARIZE(table, col, ...),
        optionally wrapped in FILTER(..., condition)."""
        if not isinstance(node, A.Call):
            return None
        if node.name == "FILTER" and len(node.args) == 2:
            inner = self._column_iter(node.args[0], ctx)
            if inner is None:
                return None
            cols, cond, ctx = inner
            return cols, node.args[1] if cond is None else A.Binary("&&", cond, node.args[1]), ctx

        def columns(args: tuple[A.Node, ...]) -> list[Column] | None:
            out = []
            for a in args:
                target = self._resolve_colref(a) if isinstance(a, A.ColumnRef) else None
                if not isinstance(target, Column):
                    return None
                out.append(target)
            return out or None

        if node.name in ("VALUES", "DISTINCT") and len(node.args) == 1:
            cols = columns(node.args)
            return (cols, None, ctx) if cols else None
        if node.name == "ALL" and node.args:
            cols = columns(node.args)
            if not cols:
                return None
            keys = frozenset((_k(c.table), _k(c.name)) for c in cols)
            return cols, None, self._remove(ctx, columns=keys)
        if node.name == "SUMMARIZE" and len(node.args) >= 2:
            table = self._table_of(node.args[0])
            cols = columns(node.args[1:])
            if table is None or not cols:
                return None
            # Only combinations that exist in the table are iterated.
            exists = A.Binary(
                ">", A.Call("CALCULATE", (A.Call("COUNTROWS", (A.TableRef(table.name),)),)), A.Num("0")
            )
            return cols, exists, ctx
        return None

    def _needs_transition(self, node: A.Node) -> bool:
        """True if evaluating `node` per row needs context transition."""
        for n in A.walk(node):
            if isinstance(n, A.BareRef) and self._find_measure(n.name) is not None:
                return True
            if isinstance(n, A.ColumnRef) and self.model.has_table(n.table) \
                    and self.model.table(n.table).column(n.column) is None \
                    and self._find_measure(n.column) is not None:
                return True
            if isinstance(n, A.Call) and n.name in _AGG_FUNCS:
                return True
        return False

    def _qualify(self, node: A.Node, table: Table) -> A.Node:
        """Turn [Column] into Table[Column] and drop RELATED, for a row of `table`."""
        q = lambda n: self._qualify(n, table)  # noqa: E731
        if isinstance(node, A.BareRef):
            if self._find_measure(node.name) is None and table.column(node.name) is not None:
                return A.ColumnRef(table.name, table.column(node.name).name)
            return node
        if isinstance(node, A.Call):
            if node.name == "RELATED" and len(node.args) == 1:
                return q(node.args[0])
            return A.Call(node.name, tuple(q(a) for a in node.args))
        if isinstance(node, A.Unary):
            return A.Unary(node.op, q(node.operand))
        if isinstance(node, A.Binary):
            return A.Binary(node.op, q(node.left), q(node.right))
        if isinstance(node, A.SetLiteral):
            return A.SetLiteral(tuple(q(a) for a in node.items))
        if isinstance(node, A.VarBlock):
            return A.VarBlock(tuple((n, q(e)) for n, e in node.vars), q(node.body))
        return node

    def _row_iter(
        self, src: A.Node, ctx: Ctx, exprs: list[A.Node],
    ) -> tuple[list[Column], A.Node | None, Ctx, list[A.Node]] | None:
        """Iterate the rows of a table (optionally filtered) with context
        transition. A row is identified by the table's key column, so the
        rows are the values of the key plus the columns the expressions read."""
        conds: list[A.Node] = []
        node = src
        while isinstance(node, A.Call) and node.name == "FILTER" and len(node.args) == 2:
            conds.append(node.args[1])
            node = node.args[0]
        remove = False
        if isinstance(node, A.Call) and node.name in ("ALL", "VALUES", "DISTINCT") and len(node.args) == 1:
            remove = node.name == "ALL"
            node = node.args[0]
        table = self._table_of(node)
        if table is None:
            return None
        key = self.model.key_column(table.name)
        if key is None:
            raise UnsupportedError(
                f"iterating '{table.name}' row by row with a measure needs a key column: mark one with "
                "isKey, relate another table to it, or iterate VALUES of a column instead"
            )
        if remove:
            ctx = self._remove(ctx, tables=self.model.expanded(table.name))
        reach = self.model.paths_from(table.name)
        exprs = [self._qualify(e, table) for e in exprs]
        conds = [self._qualify(c, table) for c in conds]
        cols, seen = [key], {(_k(table.name), _k(key.name))}
        for e in exprs + conds:
            for n in A.walk(e):
                if isinstance(n, A.ColumnRef) and self.model.has_table(n.table) and _k(n.table) in reach:
                    col = self.model.table(n.table).column(n.column)
                    if col is not None and (_k(col.table), _k(col.name)) not in seen:
                        seen.add((_k(col.table), _k(col.name)))
                        cols.append(col)
        cond = None
        for c in conds:
            cond = c if cond is None else A.Binary("&&", cond, c)
        return cols, cond, ctx, exprs

    def _iter_plan(self, src: A.Node, ctx: Ctx, expr: A.Node) -> dict | None:
        """Decide whether an iterator needs an inner query. Returns None when
        the plain row-by-row path (no measures involved) is enough."""
        top = None
        order: A.Node | None = None
        if isinstance(src, A.Call) and src.name == "TOPN":
            if len(src.args) not in (3, 4):
                raise UnsupportedError("TOPN is supported as TOPN(n, table, order expression [, ASC|DESC])")
            n = self._int_literal(src.args[0])
            if n is None or n < 1:
                raise UnsupportedError("TOPN needs a literal positive number of rows")
            order = src.args[2]
            asc = len(src.args) == 4 and self._is_ascending(src.args[3])
            top, src = (n, asc), src.args[1]
        exprs = [expr] + ([order] if order is not None else [])
        it = self._column_iter(src, ctx)
        if it is not None:
            cols, cond, ctx2 = it
        else:
            probe = src
            conds = []
            while isinstance(probe, A.Call) and probe.name == "FILTER" and len(probe.args) == 2:
                conds.append(probe.args[1])
                probe = probe.args[0]
            if top is None and not any(self._needs_transition(e) for e in exprs + conds):
                return None
            found = self._row_iter(src, ctx, exprs)
            if found is None:
                if top is not None:
                    raise UnsupportedError("TOPN over this table expression is not supported yet")
                return None
            cols, cond, ctx2, exprs = found
        if cond is not None:
            exprs = [A.Call("IF", (cond, e)) for e in exprs]
        return {"cols": cols, "exprs": exprs, "ctx": ctx2, "top": top}

    @staticmethod
    def _is_ascending(node: A.Node) -> bool:
        if isinstance(node, A.NameRef):
            return node.name.upper() == "ASC"
        if isinstance(node, A.Bool):
            return node.value
        return isinstance(node, A.Num) and node.text not in ("0", "0.0")

    def _nested_sql(
        self, iter_cols: list[Column], exprs: list[A.Node], ctx: Ctx,
        env: dict[str, _Var], stack: tuple[str, ...],
    ) -> tuple[str, tuple[int, ...]]:
        """SQL of an inner query grouped by the outer keys plus the iterated
        columns. Its columns are g0..gN (outer keys first), then v (and o)."""
        if ctx.rowctx:
            raise UnsupportedError("an iterator nested inside another column iterator is not supported yet")
        if ctx.time_ops and ctx.time_col is not None and any(
            _k(c.table) == ctx.time_col[0] for c in iter_cols
        ):
            raise UnsupportedError(
                "iterating date-table columns inside time intelligence is not supported yet"
            )
        for e in exprs:
            for n in A.walk(e):
                if isinstance(n, A.NameRef):
                    var = env.get(_k(n.name))
                    if var is not None and var.ast is None:
                        raise UnsupportedError(
                            f"variable '{n.name}' holds an aggregated value and is used inside an "
                            "iterator, which is not supported yet"
                        )
        cols = [self.group_cols[i] for i in ctx.group]
        n_outer = len(cols)
        row_cols: set[int] = set()
        for c in iter_cols:
            key = (_k(c.table), _k(c.name))
            pos = next((j for j, a in enumerate(cols) if (_k(a.table), _k(a.name)) == key), None)
            if pos is None:
                cols.append(c)
                pos = len(cols) - 1
            row_cols.add(pos)

        sub = self._child()
        sub.group_cols = cols
        sub.row_cols = frozenset(row_cols)
        sub.query_filters = ctx.filters
        sub_ctx = Ctx(tuple(range(n_outer)), ctx.filters, ctx.time_col, ctx.time_ops,
                      rowctx=True, userel=ctx.userel)
        env_sub = {k: v for k, v in env.items() if v.ast is not None}
        values = [sub._scalar(e, sub_ctx, env_sub, stack) for e in exprs]
        ctes, from_ = sub._core()
        select = [f'"s"."g{j}" AS "g{j}"' for j in range(len(cols))]
        select += [f'{v} AS "{name}"' for v, name in zip(values, ("v", "o"))]
        sql = ("WITH " + ",\n".join(ctes) + "\n" if ctes else "") + "SELECT " + ", ".join(select)
        return sql + "\n" + "\n".join(from_), ctx.group

    def _nested(self, agg: str, plan: dict, env: dict[str, _Var], stack: tuple[str, ...]) -> str:
        """Compile `AGGX(<rows>, expr)` as an aggregate over an inner query."""
        sql, keys = self._nested_sql(plan["cols"], plan["exprs"], plan["ctx"], env, stack)
        top = plan["top"]
        leaf = NestedLeaf(agg, sql, keys, top[0] if top else None, bool(top and top[1]))
        idx = self.leaves.setdefault(leaf, len(self.leaves))
        return f'"l{idx}"."v"'

    def _rankx(self, node: A.Call, ctx: Ctx, env: dict[str, _Var], stack: tuple[str, ...]) -> str:
        """RANKX(table, expr [, value [, order [, ties]]]): one plus the number
        of rows of `table` whose expr beats the current value."""
        if not 2 <= len(node.args) <= 5:
            raise CompileError("RANKX takes 2 to 5 arguments")
        args = list(node.args) + [A.Call("BLANK", ())] * (5 - len(node.args))
        blank = lambda n: isinstance(n, A.Call) and n.name == "BLANK"  # noqa: E731
        it = self._column_iter(args[0], ctx)
        if it is not None:
            cols, cond, ctx2 = it
            expr = args[1]
        else:
            found = self._row_iter(args[0], ctx, [args[1]])
            if found is None:
                raise UnsupportedError("RANKX over this table expression is not supported yet")
            cols, cond, ctx2, (expr,) = found
        if cond is not None:
            expr = A.Call("IF", (cond, expr))
        sql, keys = self._nested_sql(cols, [expr], ctx2, env, stack)
        idx = self.leaves.setdefault(TableLeaf(sql, keys), len(self.leaves))
        value = self._scalar(args[1] if blank(args[2]) else args[2], ctx, env, stack)
        asc = not blank(args[3]) and self._is_ascending(args[3])
        dense = isinstance(args[4], A.NameRef) and args[4].name.upper() == "DENSE"
        match = "".join(
            self.d.null_safe_eq(f'"n"."g{j}"', f'"s"."g{i}"') + " AND " for j, i in enumerate(keys)
        )
        count = 'COUNT(DISTINCT "n"."v")' if dense else "COUNT(*)"
        return (
            f"(CASE WHEN {value} IS NULL THEN NULL ELSE "
            f'(SELECT {count} + 1 FROM "l{idx}" AS "n" WHERE {match}"n"."v" {"<" if asc else ">"} {value}) END)'
        )

    # -- table arguments (iterators) -------------------------------------
    def _table_arg(self, node: A.Node, ctx: Ctx, env: dict[str, _Var]) -> tuple[str, tuple[A.Node, ...], Ctx]:
        table = self._table_of(node)
        if table is not None:
            return _k(table.name), (), ctx
        if isinstance(node, A.Call):
            if node.name == "FILTER":
                self._arity(node, 2)
                base, rf, c = self._table_arg(node.args[0], ctx, env)
                return base, rf + (self._rowify(node.args[1], base, env, {}),), c
            if node.name in ("ALL", "VALUES", "DISTINCT") and len(node.args) == 1:
                inner = self._table_of(node.args[0])
                if inner is not None:
                    if node.name == "ALL":
                        ctx = self._remove(ctx, tables=self.model.expanded(inner.name))
                    return _k(inner.name), (), ctx
                raise UnsupportedError(
                    f"iterating over {node.name}(<column>) needs context transition, which is not supported yet"
                )
            if node.name == "CALCULATETABLE" and node.args:
                return self._table_arg(node.args[0], self._apply_filters(ctx, node.args[1:], env), env)
            raise UnsupportedError(f"table expression {node.name}(...) is not supported as an iterator source")
        raise CompileError("expected a table")

    # -- row-level expressions -------------------------------------------
    def _rowify(self, node: A.Node, base: str | None, env: dict[str, _Var], rowvars: dict[str, A.Node]) -> A.Node:
        """Rewrite a row-context expression into canonical form: every column
        fully qualified, variables inlined, RELATED removed."""
        rec = lambda n: self._rowify(n, base, env, rowvars)  # noqa: E731
        if isinstance(node, (A.Num, A.Str, A.Bool)):
            return node
        if isinstance(node, A.BareRef):
            if base is not None:
                col = self.model.table(base).column(node.name)
                if col is not None:
                    c = self._column(col.table, col.name)
                    return A.ColumnRef(c.table, c.name)
            if self._find_measure(node.name) is not None:
                raise UnsupportedError(
                    f"measure [{node.name}] is referenced inside a row context "
                    "(context transition), which is not supported yet"
                )
            raise CompileError(f"unknown column [{node.name}]")
        if isinstance(node, A.ColumnRef):
            target = self._resolve_colref(node)
            if isinstance(target, Measure):
                raise UnsupportedError(
                    f"measure [{target.name}] is referenced inside a row context "
                    "(context transition), which is not supported yet"
                )
            if base is not None and _k(target.table) not in self.model.paths_from(base):
                raise CompileError(
                    f"'{target.table}'[{target.name}] cannot be reached from "
                    f"'{self.model.table(base).name}' through a many-to-one relationship"
                )
            return A.ColumnRef(target.table, target.name)
        if isinstance(node, A.NameRef):
            key = _k(node.name)
            if key in rowvars:
                return rowvars[key]
            var = env.get(key)
            if var is not None:
                if var.ast is None:
                    raise UnsupportedError(
                        f"variable '{node.name}' holds an aggregated value and is used inside a row "
                        "context, which is not supported yet"
                    )
                return var.ast
            raise CompileError(f"unknown name '{node.name}'")
        if isinstance(node, A.VarBlock):
            rv = dict(rowvars)
            for name, expr in node.vars:
                rv[_k(name)] = self._rowify(expr, base, env, rv)
            return self._rowify(node.body, base, env, rv)
        if isinstance(node, A.Unary):
            return A.Unary(node.op, rec(node.operand))
        if isinstance(node, A.Binary):
            return A.Binary(node.op, rec(node.left), rec(node.right))
        if isinstance(node, A.SetLiteral):
            return A.SetLiteral(tuple(rec(i) for i in node.items))
        if isinstance(node, A.Call):
            if node.name == "RELATED":
                self._arity(node, 1)
                return rec(node.args[0])
            if node.name in _AGG_FUNCS:
                raise UnsupportedError(
                    f"{node.name} inside a row context needs context transition, which is not supported yet"
                )
            return A.Call(node.name, tuple(rec(a) for a in node.args))
        raise CompileError("expression is not valid in a row context")

    def _row_sql(self, node: A.Node, colsql: Callable[[str, str], str] | None = None) -> str:
        colsql = colsql or self._colsql
        if isinstance(node, A.ColumnRef):
            col = self.model.column(node.table, node.column)
            if col.expression is not None:
                inner = self._row_sql(self._calc_ast(col), colsql)
                key = self.model.key_column(col.table)
                if key is not None and key.expression is None:
                    # No calculated value on the blank row that stands in for
                    # fact rows without a matching row in this table.
                    return f"(CASE WHEN {colsql(key.table, key.name)} IS NULL THEN NULL ELSE {inner} END)"
                return f"({inner})"
            return colsql(node.table, node.column)
        return self._pure(node, lambda n: self._row_sql(n, colsql))

    # -- pure scalar operators and functions -----------------------------
    def _pure(self, node: A.Node, rec: Callable[[A.Node], str]) -> str:
        d = self.d
        if isinstance(node, A.Num):
            return node.text
        if isinstance(node, A.Str):
            return d.string(node.value)
        if isinstance(node, A.Bool):
            return "TRUE" if node.value else "FALSE"
        if isinstance(node, A.Unary):
            x = rec(node.operand)
            if node.op == "-":
                return f"(-{x})"
            if node.op == "+":
                return x
            return f"(NOT {x})"
        if isinstance(node, A.Binary):
            op = node.op
            if op == "IN":
                if not isinstance(node.right, A.SetLiteral) or not node.right.items:
                    raise UnsupportedError("IN is supported with a non-empty list such as {1, 2, 3}")
                items = ", ".join(rec(i) for i in node.right.items)
                return f"({rec(node.left)} IN ({items}))"
            a, b = rec(node.left), rec(node.right)
            if op in ("+", "-"):
                # DAX: BLANK counts as 0 in addition, but BLANK + BLANK is BLANK.
                a_lit, b_lit = isinstance(node.left, A.Num), isinstance(node.right, A.Num)
                if a_lit and b_lit:
                    return f"({a} {op} {b})"
                if a_lit:
                    return f"({a} {op} COALESCE({b}, 0))"
                if b_lit:
                    return f"(COALESCE({a}, 0) {op} {b})"
                return (
                    f"(CASE WHEN {a} IS NULL AND {b} IS NULL THEN NULL "
                    f"ELSE COALESCE({a}, 0) {op} COALESCE({b}, 0) END)"
                )
            if op == "*":
                return f"({a} * {b})"
            if op == "/":
                return f"(CAST({a} AS DOUBLE) / NULLIF({b}, 0))"
            if op == "^":
                return f"POWER({a}, {b})"
            if op == "&":
                return f"(COALESCE(CAST({a} AS VARCHAR), '') || COALESCE(CAST({b} AS VARCHAR), ''))"
            if op in ("=", "<>", "<", "<=", ">", ">="):
                return f"({a} {op} {b})"
            if op == "&&":
                return f"({a} AND {b})"
            if op == "||":
                return f"({a} OR {b})"
            raise CompileError(f"unknown operator {op}")
        if isinstance(node, A.Call):
            return self._pure_call(node, rec)
        if isinstance(node, A.SetLiteral):
            raise UnsupportedError("a table constructor {...} is only supported on the right of IN")
        raise CompileError(f"cannot compile {type(node).__name__} here")

    def _pure_call(self, node: A.Call, rec: Callable[[A.Node], str]) -> str:
        name, args = node.name, node.args
        if name not in _PURE_FUNCS:
            if name in _KNOWN_UNSUPPORTED or name in _TIME_FUNCS or name in (
                "ALL", "REMOVEFILTERS", "ALLEXCEPT", "KEEPFILTERS", "FILTER"
            ):
                raise UnsupportedError(f"{name} is not supported in this position yet")
            raise UnsupportedError(f"DAX function {name} is not supported yet")
        a = [rec(x) for x in args] if name != "SWITCH" else []
        n = len(args)

        def need(*allowed: int) -> None:
            self._arity(node, *allowed)

        if name == "IF":
            need(2, 3)
            other = a[2] if n == 3 else "NULL"
            return f"(CASE WHEN {a[0]} THEN {a[1]} ELSE {other} END)"
        if name == "SWITCH":
            if n < 3:
                raise CompileError("SWITCH needs an expression and at least one value/result pair")
            subject = args[0]
            rest = [rec(x) for x in args[1:]]
            pairs, default = rest, "NULL"
            if len(rest) % 2 == 1:
                pairs, default = rest[:-1], rest[-1]
            if isinstance(subject, A.Bool) and subject.value or (
                isinstance(subject, A.Call) and subject.name == "TRUE"
            ):
                whens = " ".join(f"WHEN {pairs[i]} THEN {pairs[i + 1]}" for i in range(0, len(pairs), 2))
            else:
                s = rec(subject)
                whens = " ".join(f"WHEN {s} = {pairs[i]} THEN {pairs[i + 1]}" for i in range(0, len(pairs), 2))
            return f"(CASE {whens} ELSE {default} END)"
        if name == "DIVIDE":
            need(2, 3)
            alt = a[2] if n == 3 else "NULL"
            return (
                f"(CASE WHEN {a[1]} IS NULL OR {a[1]} = 0 THEN {alt} "
                f"ELSE CAST({a[0]} AS DOUBLE) / {a[1]} END)"
            )
        if name == "COALESCE":
            if n < 2:
                raise CompileError("COALESCE needs at least 2 arguments")
            return f"COALESCE({', '.join(a)})"
        if name == "ISBLANK":
            need(1)
            return f"({a[0]} IS NULL)"
        if name == "BLANK":
            need(0)
            return "NULL"
        if name in ("TRUE", "FALSE"):
            need(0)
            return name
        if name == "AND":
            need(2)
            return f"({a[0]} AND {a[1]})"
        if name == "OR":
            need(2)
            return f"({a[0]} OR {a[1]})"
        if name == "NOT":
            need(1)
            return f"(NOT {a[0]})"
        if name in ("MIN", "MAX"):
            need(2)
            return f"{'LEAST' if name == 'MIN' else 'GREATEST'}({a[0]}, {a[1]})"
        if name == "ROUND":
            need(2)
            return f"ROUND({a[0]}, {a[1]})"
        if name == "POWER":
            need(2)
            return f"POWER({a[0]}, {a[1]})"
        if name == "MOD":
            need(2)
            return f"MOD({a[0]}, {a[1]})"
        one_arg = {"ABS": "ABS", "INT": "FLOOR", "SQRT": "SQRT", "EXP": "EXP", "LN": "LN",
                   "UPPER": "UPPER", "LOWER": "LOWER", "LEN": "LENGTH", "TRIM": "TRIM"}
        if name in one_arg:
            need(1)
            return f"{one_arg[name]}({a[0]})"
        if name in ("YEAR", "QUARTER", "MONTH", "DAY"):
            need(1)
            return f"EXTRACT({name} FROM {a[0]})"
        if name == "DATE":
            need(3)
            if all(isinstance(x, A.Num) and x.text.isdigit() for x in args):
                y, m, dd = (int(x.text) for x in args)
                if 1 <= m <= 12 and 1 <= dd <= 31:  # a literal date is portable across databases
                    return f"CAST('{y:04d}-{m:02d}-{dd:02d}' AS DATE)"
            return self.d.make_date(*a)
        if name in ("LEFT", "RIGHT"):
            need(1, 2)
            return f"{name}({a[0]}, {a[1] if n == 2 else 1})"
        if name == "CONCATENATE":
            need(2)
            return f"(COALESCE(CAST({a[0]} AS VARCHAR), '') || COALESCE(CAST({a[1]} AS VARCHAR), ''))"
        if name in ("USERNAME", "USERPRINCIPALNAME"):
            need(0)
            if self.user is None:
                raise CompileError(f"{name}() needs a signed-in user; pass one with --user")
            return self.d.string(self.user)
        if name == "CONTAINSSTRING":
            need(2)
            return f"(POSITION(LOWER({a[1]}) IN LOWER({a[0]})) > 0)"
        raise UnsupportedError(f"DAX function {name} is not supported yet")

    # -- filter context --------------------------------------------------
    def _bool_filter(self, node: A.Node, env: dict[str, _Var]) -> Filter | None:
        """Turn a boolean expression over columns into a Filter."""
        expr = self._rowify(node, None, env, {})
        cols = frozenset(
            (_k(n.table), _k(n.column)) for n in A.walk(expr) if isinstance(n, A.ColumnRef)
        )
        if not cols:
            return None
        return Filter(cols, expr)

    def _remove(
        self,
        ctx: Ctx,
        tables: frozenset[str] = frozenset(),
        columns: frozenset[ColKey] = frozenset(),
        everything: bool = False,
        keep: frozenset[ColKey] = frozenset(),
    ) -> Ctx:
        def col_removed(key: ColKey) -> bool:
            if key in keep:
                return False
            return everything or key[0] in tables or key in columns

        group = tuple(
            i for i in ctx.group
            if not col_removed((_k(self.group_cols[i].table), _k(self.group_cols[i].name)))
        )
        filters = tuple(f for f in ctx.filters if not all(col_removed(c) for c in f.columns))
        new = replace(ctx, group=group, filters=filters)
        if ctx.time_ops and ctx.time_col is not None:
            t = ctx.time_col[0]
            if everything or t in tables:
                if keep and any(c[0] == t for c in keep):
                    raise UnsupportedError("ALLEXCEPT on the date table inside time intelligence")
                new = replace(new, time_col=None, time_ops=())
            elif any(c[0] == t for c in columns):
                raise UnsupportedError(
                    "removing a filter from one date column inside time intelligence is not supported yet"
                )
        return new

    def _apply_filters(self, ctx: Ctx, fargs: tuple[A.Node, ...], env: dict[str, _Var]) -> Ctx:
        rm_tables: set[str] = set()
        rm_cols: set[ColKey] = set()
        rm_all = False
        keep: set[ColKey] = set()
        adds: list[Filter] = []
        time: list[tuple[ColKey, TimeOp]] = []
        userel: set[str] = set()

        def removal_targets(args: tuple[A.Node, ...], fn: str) -> None:
            nonlocal rm_all
            if not args:
                rm_all = True
                return
            for a in args:
                t = self._table_of(a)
                if t is not None:
                    rm_tables.update(self.model.expanded(t.name))
                elif isinstance(a, A.ColumnRef):
                    c = self._resolve_colref(a)
                    if isinstance(c, Measure):
                        raise CompileError(f"{fn} expects tables or columns")
                    rm_cols.add((_k(c.table), _k(c.name)))
                else:
                    raise CompileError(f"{fn} expects tables or columns")

        def add_bool(node: A.Node, override: bool) -> None:
            top = self._time_from_predicate(node)
            if top is not None:
                time.append(top)
                return
            flt = self._bool_filter(node, env)
            if flt is None:
                raise CompileError("a CALCULATE filter must reference a column")
            adds.append(flt)
            if override:
                rm_cols.update(flt.columns)

        def classify(arg: A.Node, override: bool) -> None:
            if isinstance(arg, A.Call):
                name = arg.name
                if name in ("ALL", "REMOVEFILTERS"):
                    removal_targets(arg.args, name)
                    return
                if name == "ALLEXCEPT":
                    if len(arg.args) < 2 or self._table_of(arg.args[0]) is None:
                        raise CompileError("ALLEXCEPT expects a table followed by columns to keep")
                    rm_tables.update(self.model.expanded(self._table_of(arg.args[0]).name))
                    for c in arg.args[1:]:
                        col = self._resolve_colref(c) if isinstance(c, A.ColumnRef) else None
                        if not isinstance(col, Column):
                            raise CompileError("ALLEXCEPT expects columns after the table")
                        keep.add((_k(col.table), _k(col.name)))
                    return
                if name == "KEEPFILTERS":
                    self._arity(arg, 1)
                    classify(arg.args[0], False)
                    return
                if name == "USERELATIONSHIP":
                    self._arity(arg, 2)
                    ends = [self._resolve_colref(a) if isinstance(a, A.ColumnRef) else None for a in arg.args]
                    if not all(isinstance(e, Column) for e in ends):
                        raise CompileError("USERELATIONSHIP expects two columns")
                    rel = self.model.find_relationship(
                        (ends[0].table, ends[0].name), (ends[1].table, ends[1].name))
                    if rel is None:
                        raise CompileError(
                            f"no relationship between '{ends[0].table}'[{ends[0].name}] and "
                            f"'{ends[1].table}'[{ends[1].name}]"
                        )
                    userel.add(rel.name)
                    return
                if name == "DATESBETWEEN":
                    self._arity(arg, 3)
                    col = self._date_col(arg.args[0], name)
                    c = self.model.column(*col)
                    ref: A.Node = A.ColumnRef(c.table, c.name)
                    parts = []
                    for op, bound in ((">=", arg.args[1]), ("<=", arg.args[2])):
                        if isinstance(bound, A.Call) and bound.name == "BLANK":
                            continue
                        if not self._const_with(bound, env):
                            raise UnsupportedError("DATESBETWEEN needs fixed dates such as DATE(2024, 1, 1)")
                        parts.append(A.Binary(op, ref, self._inline_consts(bound, env)))
                    rm_tables.add(col[0])  # a date filter replaces the filters on the date table
                    if parts:
                        expr = parts[0] if len(parts) == 1 else A.Binary("&&", parts[0], parts[1])
                        adds.append(Filter(frozenset({col}), expr))
                    return
                if name == "FILTER":
                    self._arity(arg, 2)
                    src, cond = arg.args
                    if isinstance(src, A.Call) and src.name == "ALL":
                        # Running total: the date context must survive, because
                        # the time operation is anchored on it.
                        top = self._time_from_predicate(cond)
                        if top is not None:
                            time.append(top)
                            return
                        removal_targets(src.args, "ALL")
                        base = self._table_of(src.args[0]) if len(src.args) == 1 else None
                    elif isinstance(src, A.Call) and src.name in ("VALUES", "DISTINCT") and len(src.args) == 1:
                        base = self._table_of(src.args[0])
                    else:
                        base = self._table_of(src)
                        if base is None:
                            raise UnsupportedError("FILTER over this table expression is not supported yet")
                    expr = self._rowify(cond, _k(base.name) if base else None, env, {})
                    cols = frozenset(
                        (_k(n.table), _k(n.column)) for n in A.walk(expr) if isinstance(n, A.ColumnRef)
                    )
                    if not cols:
                        raise CompileError("the FILTER condition must reference a column")
                    adds.append(Filter(cols, expr))
                    return
                if name in _TIME_FUNCS:
                    time.append(self._time_op(arg))
                    return
                if name in _KNOWN_UNSUPPORTED:
                    raise UnsupportedError(f"{name} as a CALCULATE filter is not supported yet")
            add_bool(arg, override)

        for arg in fargs:
            classify(arg, True)

        new = self._remove(ctx, frozenset(rm_tables), frozenset(rm_cols), rm_all, frozenset(keep))
        if new.time_ops and new.time_col is not None:
            if any(new.time_col[0] in f.tables for f in adds):
                raise UnsupportedError(
                    "a filter on the date table inside time intelligence is not supported yet"
                )
        new = replace(new, filters=new.filters + tuple(f for f in adds if f not in new.filters))
        if userel:
            new = replace(new, userel=new.userel | frozenset(userel))
        for col, op in time:
            if any(col[0] in f.tables for f in adds):
                raise UnsupportedError(
                    "combining time intelligence with another date-table filter in one CALCULATE "
                    "is not supported yet"
                )
            new = self._apply_time(new, col, op)
        return new

    def _date_col(self, node: A.Node, fn: str) -> ColKey:
        if not isinstance(node, A.ColumnRef):
            raise CompileError(f"{fn} expects a date column such as 'Date'[Date]")
        col = self._resolve_colref(node)
        if isinstance(col, Measure):
            raise CompileError(f"{fn} expects a date column")
        return (_k(col.table), _k(col.name))

    @staticmethod
    def _int_literal(node: A.Node) -> int | None:
        sign = 1
        while isinstance(node, A.Unary) and node.op in "+-":
            sign *= -1 if node.op == "-" else 1
            node = node.operand
        if isinstance(node, A.Num) and node.text.isdigit():
            return sign * int(node.text)
        return None

    def _unit(self, node: A.Node, fn: str) -> str:
        if isinstance(node, A.NameRef) and node.name.upper() in _UNITS:
            return node.name.upper()
        raise CompileError(f"{fn}: expected an interval of YEAR, QUARTER, MONTH or DAY")

    def _time_op(self, call: A.Call) -> tuple[ColKey, TimeOp]:
        name, args = call.name, call.args
        if name == "SAMEPERIODLASTYEAR":
            self._arity(call, 1)
            return self._date_col(args[0], name), TimeOp("shift", -1, "YEAR")
        if name == "DATEADD":
            self._arity(call, 3)
            n = self._int_literal(args[1])
            if n is None:
                raise UnsupportedError("DATEADD needs a literal number of intervals")
            return self._date_col(args[0], name), TimeOp("shift", n, self._unit(args[2], name))
        if name in ("DATESYTD", "DATESQTD", "DATESMTD"):
            if len(args) != 1:
                raise UnsupportedError(f"{name} with a custom year end is not supported yet")
            unit = {"Y": "YEAR", "Q": "QUARTER", "M": "MONTH"}[name[5]]
            return self._date_col(args[0], name), TimeOp("todate", 0, unit)
        if name == "DATESINPERIOD":
            self._arity(call, 4)
            col = self._date_col(args[0], name)
            anchor = args[1]
            ok = (
                isinstance(anchor, A.Call) and anchor.name in ("MAX", "LASTDATE") and len(anchor.args) == 1
                and isinstance(anchor.args[0], A.ColumnRef) and self._date_col(anchor.args[0], name) == col
            )
            n = self._int_literal(args[2])
            if not ok or n is None or n >= 0:
                raise UnsupportedError(
                    "DATESINPERIOD is supported as DATESINPERIOD(dates, MAX(dates), -N, <interval>)"
                )
            return col, TimeOp("window", n, self._unit(args[3], name))
        if name in _PERIOD_FUNCS:
            self._arity(call, 1)
            kind, unit = _PERIOD_FUNCS[name]
            return self._date_col(args[0], name), TimeOp(kind, 0, unit)
        if name == "PARALLELPERIOD":
            self._arity(call, 3)
            n = self._int_literal(args[1])
            if n is None:
                raise UnsupportedError("PARALLELPERIOD needs a literal number of intervals")
            return self._date_col(args[0], name), TimeOp("parallel", n, self._unit(args[2], name))
        raise UnsupportedError(f"{name} is not supported yet")

    def _time_from_predicate(self, node: A.Node) -> tuple[ColKey, TimeOp] | None:
        """Recognise the running-total idiom `Date[Date] <= MAX(Date[Date])`."""
        if isinstance(node, A.Binary) and node.op == "<=" and isinstance(node.left, A.ColumnRef):
            r = node.right
            if isinstance(r, A.Call) and r.name == "MAX" and len(r.args) == 1 and isinstance(r.args[0], A.ColumnRef):
                left, right = self._date_col(node.left, "filter"), self._date_col(r.args[0], "MAX")
                if left == right:
                    return left, TimeOp("todate", 0, "ALL")
        return None

    def _apply_time(self, ctx: Ctx, col: ColKey, op: TimeOp) -> Ctx:
        if ctx.time_col is not None and ctx.time_col != col:
            raise UnsupportedError("time intelligence over two different date columns")
        if ctx.time_ops and ctx.time_ops[-1].kind != "shift":
            raise UnsupportedError(
                "time intelligence applied inside a to-date or rolling-window calculation is not "
                "supported yet (the other nesting order is)"
            )
        return replace(ctx, time_col=col, time_ops=ctx.time_ops + (op,))

    # -- leaves ----------------------------------------------------------
    def _leaf(self, agg: str, base: str, expr: A.Node | None, row_filters: tuple[A.Node, ...], ctx: Ctx) -> str:
        reach = self.model.paths_from(base, ctx.userel)
        group = tuple(i for i in ctx.group if _k(self.group_cols[i].table) in reach)
        filters = tuple(f for f in ctx.filters if all(t in reach for t in f.tables))
        time_col, time_ops = ctx.time_col, ctx.time_ops
        if not time_ops or time_col is None or time_col[0] not in reach:
            time_col, time_ops = None, ()
        leaf = Leaf(agg, base, expr, row_filters, Ctx(group, filters, time_col, time_ops, userel=ctx.userel))
        idx = self.leaves.setdefault(leaf, len(self.leaves))
        return f'"l{idx}"."v"'

    def _source(self, base: str, tables: set[str], userel: frozenset[str] = frozenset()) -> tuple[list[str], list[str]]:
        """FROM clause for `base` joined (many-to-one) to the given tables,
        plus the row-level security conditions that apply. Security filters
        are added for every secured table the base reaches, whether or not
        the query mentions it, so no CALCULATE can remove them."""
        d, model = self.d, self.model
        paths = model.paths_from(base, userel)
        need: list[str] = []

        def add(table_key: str) -> None:
            for rel in paths.get(table_key, []):
                k = _k(rel.to_table)
                if k not in need:
                    need.append(k)

        for tk in tables:
            add(tk)
        where: list[str] = []
        for tk, pred in self.security.items():
            if tk in paths:
                add(tk)
                for t2 in self._expr_tables(pred):
                    add(t2)
                where.append(self._row_sql(pred))
        t0 = model.table(base)
        lines = [f"FROM {self.relation_for(t0)} AS {d.quote(t0.name)}"]
        joined: set[str] = set()
        for tk in need:
            for rel in paths[tk]:
                k = _k(rel.to_table)
                if k in joined:
                    continue
                joined.add(k)
                t = model.table(rel.to_table)
                lines.append(
                    f"LEFT JOIN {self.relation_for(t)} AS {d.quote(t.name)} "
                    f"ON {self._colsql(rel.from_table, rel.from_column)} = "
                    f"{self._colsql(rel.to_table, rel.to_column)}"
                )
        return lines, where

    def _group_ref(self, i: int) -> A.ColumnRef:
        col = self.group_cols[i]
        return A.ColumnRef(col.table, col.name)

    def _nested_leaf_sql(self, leaf: NestedLeaf) -> tuple[str, tuple[int, ...]]:
        agg = {
            "SUM": 'SUM("n"."v")', "AVG": 'AVG("n"."v")', "MIN": 'MIN("n"."v")',
            "MAX": 'MAX("n"."v")', "COUNT": 'NULLIF(COUNT("n"."v"), 0)',
        }[leaf.agg]
        keys = [f'"g{j}"' for j in range(len(leaf.keys))]
        inner = leaf.sql
        if leaf.top is not None:
            part = f"PARTITION BY {', '.join(keys)} " if keys else ""
            direction = "ASC" if leaf.top_asc else "DESC"
            inner = (
                f'SELECT * FROM (\n  SELECT "q".*, RANK() OVER ({part}ORDER BY "o" {direction} NULLS LAST) AS "rk"'
                f'\n  FROM (\n{_indent(leaf.sql, 4)}\n  ) AS "q"\n) AS "q2" WHERE "rk" <= {leaf.top}'
            )
        select = [f'"n"."g{j}" AS "g{i}"' for j, i in enumerate(leaf.keys)] + [f'{agg} AS "v"']
        sql = "SELECT " + ", ".join(select) + f'\nFROM (\n{_indent(inner)}\n) AS "n"'
        if leaf.keys:
            sql += "\nGROUP BY " + ", ".join(f'"n"."g{j}"' for j in range(len(leaf.keys)))
        return sql, leaf.keys

    def _leaf_sql(self, leaf: Leaf | NestedLeaf | TableLeaf) -> tuple[str, tuple[int, ...]]:
        if isinstance(leaf, TableLeaf):
            return leaf.sql, leaf.keys
        if isinstance(leaf, NestedLeaf):
            return self._nested_leaf_sql(leaf)
        ctx = leaf.ctx
        time_t = ctx.time_col[0] if ctx.time_col else None

        tables = self._expr_tables(leaf.expr)
        where: list[str] = []
        ctx_filters: list[Filter] = []
        for f in ctx.filters:
            if time_t is not None and f.tables == {time_t}:
                ctx_filters.append(f)
                continue
            tables |= self._expr_tables(f.expr)
            where.append(self._row_sql(f.expr))
        for rf in leaf.row_filters:
            tables |= self._expr_tables(rf)
            where.append(self._row_sql(rf))

        select: list[str] = []
        group_by: list[str] = []
        ctx_group: list[int] = []
        for i in ctx.group:
            ref = self._group_ref(i)
            if time_t is not None and _k(ref.table) == time_t:
                ctx_group.append(i)
                expr = f'"dctx"."g{i}"'
            else:
                tables |= self._expr_tables(ref)
                expr = self._row_sql(ref)
            select.append(f'{expr} AS "g{i}"')
            group_by.append(expr)
        if time_t is not None:
            tables.add(time_t)

        from_, secured = self._source(leaf.base, tables, ctx.userel)
        where = secured + where
        if time_t is not None:
            from_.append(self._time_join(leaf, ctx_filters, ctx_group))

        x = self._row_sql(leaf.expr) if leaf.expr is not None else None
        agg = {
            "SUM": f"SUM({x})",
            "AVG": f"AVG({x})",
            "MIN": f"MIN({x})",
            "MAX": f"MAX({x})",
            "COUNT": f"NULLIF(COUNT({x}), 0)",
            "DCOUNT": f"NULLIF(COUNT(DISTINCT {x}) + MAX(CASE WHEN {x} IS NULL THEN 1 ELSE 0 END), 0)",
            "DCOUNT_NB": f"NULLIF(COUNT(DISTINCT {x}), 0)",
            "COUNTROWS": "NULLIF(COUNT(*), 0)",
            "ONEVALUE": f"CASE WHEN COUNT(DISTINCT {x}) = 1 THEN MIN({x}) END",
            "HASONE": f"(COUNT(DISTINCT {x}) = 1)",
        }[leaf.agg]
        select.append(f'{agg} AS "v"')

        parts = ["SELECT " + ", ".join(select)] + from_
        if where:
            parts.append("WHERE " + " AND ".join(where))
        if group_by:
            parts.append("GROUP BY " + ", ".join(group_by))
        return "\n".join(parts), ctx.group

    def _time_join(self, leaf: Leaf, ctx_filters: list[Filter], ctx_group: list[int]) -> str:
        """Join the fact's actual date to the dates of the outer context.

        `dctx` holds the date-table part of the context (its filters and its
        group-by columns). A shift joins each actual date to the context date
        it was shifted from; the other operations join a date range derived
        from the first ("d0") and last ("d") context dates.
        """
        d, model = self.d, self.model
        ctx = leaf.ctx
        t_key, c_key = ctx.time_col  # type: ignore[misc]
        table = model.table(t_key)
        date_sql = d.to_date(self._row_sql(A.ColumnRef(table.name, model.column(t_key, c_key).name)))
        shifts = [op for op in ctx.time_ops if op.kind == "shift"]
        rng = ctx.time_ops[-1] if ctx.time_ops[-1].kind != "shift" else None

        refs = [self._group_ref(i) for i in ctx_group]
        tables: set[str] = set()
        for f in ctx_filters:
            tables |= self._expr_tables(f.expr)
        for ref in refs:
            tables |= self._expr_tables(ref)
        lines, conds = self._source(t_key, tables)
        conds = conds + [self._row_sql(f.expr) for f in ctx_filters]
        inner_from = " ".join(lines) + (" WHERE " + " AND ".join(conds) if conds else "")
        gcols = [f'{self._row_sql(ref)} AS "g{i}"' for ref, i in zip(refs, ctx_group)]

        if rng is None:
            inner = f"SELECT {', '.join([date_sql + ' AS \"d\"'] + gcols)} {inner_from}"
            actual = date_sql
            for op in reversed(shifts):
                actual = d.date_add(actual, -op.n, op.unit)
            cond = f'"dctx"."d" = {actual}'
        else:
            bounds = [f'MIN({date_sql}) AS "d0"', f'MAX({date_sql}) AS "d"']
            inner = f"SELECT {', '.join(gcols + bounds)} {inner_from}"
            if refs:
                inner += " GROUP BY " + ", ".join(self._row_sql(ref) for ref in refs)
            first, last = '"dctx"."d0"', '"dctx"."d"'
            for op in shifts:
                first, last = d.date_add(first, op.n, op.unit), d.date_add(last, op.n, op.unit)
            if rng.kind == "todate":
                cond = f"{date_sql} <= {last}"
                if rng.unit != "ALL":
                    cond += f" AND {date_sql} >= {d.date_trunc(rng.unit, last)}"
            elif rng.kind == "window":  # rolling window of |n| units ending at the last date
                cond = f"{date_sql} <= {last} AND {date_sql} > {d.date_add(last, rng.n, rng.unit)}"
            elif rng.kind == "prev":  # the whole period before the one holding the first date
                start = d.date_trunc(rng.unit, first)
                cond = f"{date_sql} >= {d.date_add(start, -1, rng.unit)} AND {date_sql} < {start}"
            elif rng.kind == "next":  # the whole period after the one holding the last date
                start = d.date_add(d.date_trunc(rng.unit, last), 1, rng.unit)
                cond = f"{date_sql} >= {start} AND {date_sql} < {d.date_add(start, 1, rng.unit)}"
            else:  # parallel: whole periods, moved by n
                cond = (
                    f"{date_sql} >= {d.date_add(d.date_trunc(rng.unit, first), rng.n, rng.unit)} AND "
                    f"{date_sql} < {d.date_add(d.date_trunc(rng.unit, last), rng.n + 1, rng.unit)}"
                )
        return f'JOIN ({inner}) AS "dctx" ON {cond}'

    # -- final assembly --------------------------------------------------
    def _core(self) -> tuple[list[str], list[str]]:
        """The CTEs (one per leaf, plus the spine "s") and the FROM clause
        that joins every leaf to the spine."""
        d = self.d
        n_group = len(self.group_cols)
        ctes: list[str] = []
        leaves: list[tuple[object, int]] = []  # the leaves joined to the spine
        leaf_keys: list[tuple[int, ...]] = []
        for leaf, idx in sorted(self.leaves.items(), key=lambda kv: kv[1]):
            sql, keys = self._leaf_sql(leaf)
            ctes.append(f'"l{idx}" AS (\n{_indent(sql)}\n)')
            if not isinstance(leaf, TableLeaf):
                leaves.append((leaf, idx))
                leaf_keys.append(keys)

        if n_group == 0:
            from_ = ['FROM (SELECT 1 AS "one") AS "s"']
            from_ += [f'CROSS JOIN "l{idx}"' for _, idx in leaves]
            return ctes, from_

        all_keys = tuple(range(n_group))
        if leaves and all(k == all_keys for k in leaf_keys):
            spine = "\nUNION\n".join(
                "SELECT " + ", ".join(f'"g{i}"' for i in all_keys) + f' FROM "l{idx}"'
                for _, idx in leaves
            )
        else:
            # Some measure is not at the full grain (or there is none), so
            # take every combination of the dimension values, plus the keys
            # of the full-grain leaves so that blank members (fact rows
            # with no matching dimension row) are not lost.
            parts = [self._dimension_spine()] + [
                "SELECT " + ", ".join(f'"g{i}"' for i in all_keys) + f' FROM "l{idx}"'
                for (_, idx), k in zip(leaves, leaf_keys) if k == all_keys
            ]
            spine = "\nUNION\n".join(parts)
        ctes.append(f'"s" AS (\n{_indent(spine)}\n)')
        from_ = ['FROM "s"']
        for (_, idx), keys in zip(leaves, leaf_keys):
            if keys:
                on = " AND ".join(d.null_safe_eq(f'"s"."g{i}"', f'"l{idx}"."g{i}"') for i in keys)
                from_.append(f'LEFT JOIN "l{idx}" ON {on}')
            else:
                from_.append(f'CROSS JOIN "l{idx}"')
        return ctes, from_

    def _assemble(self, query: Query, measures: list[tuple[str, str, Measure | None]]) -> CompiledQuery:
        d = self.d
        n_group = len(self.group_cols)
        ctes, from_ = self._core()

        # Output names: plain column name unless two outputs would collide.
        names = [c.name for c in self.group_cols] + [m[0] for m in measures]
        folded = [n.casefold() for n in names]
        out_cols: list[OutputColumn] = []
        for c in self.group_cols:
            name = c.name if folded.count(c.name.casefold()) == 1 else f"{c.table}[{c.name}]"
            out_cols.append(OutputColumn(name, "dimension", c.format_string, f"{c.table}[{c.name}]"))
        for name, _, m in measures:
            if m is None:
                out_cols.append(OutputColumn(name, "measure", None, name, trust="ad hoc"))
            else:
                out_cols.append(OutputColumn(name, "measure", m.format_string, name, m.trust, m.owner))

        select: list[str] = [f'"s"."g{i}" AS {d.quote(out_cols[i].name)}' for i in range(n_group)]
        select += [f"{sql} AS {d.quote(name)}" for name, sql, _ in measures]

        body = "SELECT " + ",\n       ".join(select) + "\n" + "\n".join(from_)
        outer = [f'SELECT * FROM (\n{_indent(body)}\n) AS "r"']
        if measures:
            outer.append("WHERE " + " OR ".join(f"{d.quote(m[0])} IS NOT NULL" for m in measures))
        order = self._order_by(query, out_cols)
        if order:
            outer.append("ORDER BY " + ", ".join(order))
        if query.limit is not None:
            if not isinstance(query.limit, int) or query.limit < 0:
                raise CompileError("limit must be a non-negative integer")
            outer.append(f"LIMIT {query.limit}")
        sql = ("WITH " + ",\n".join(ctes) + "\n" if ctes else "") + "\n".join(outer)
        return CompiledQuery(sql, out_cols)

    def _dimension_spine(self) -> str:
        """All combinations of the group-by values, one sub-select per table."""
        d = self.d
        by_table: dict[str, list[int]] = {}
        for i, c in enumerate(self.group_cols):
            by_table.setdefault(_k(c.table), []).append(i)
        parts = []
        for n, (tk, idxs) in enumerate(by_table.items()):
            refs = [self._group_ref(i) for i in idxs]
            filters = [f for f in self.query_filters if f.tables == {tk}]
            tables: set[str] = set()
            for node in refs + [f.expr for f in filters]:
                tables |= self._expr_tables(node)
            lines, conds = self._source(tk, tables)
            conds = conds + [self._row_sql(f.expr) for f in filters]
            cols = ", ".join(f'{self._row_sql(ref)} AS "g{i}"' for ref, i in zip(refs, idxs))
            sql = f"SELECT DISTINCT {cols} " + " ".join(lines)
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            parts.append(f'({sql}) AS "d{n}"')
        return "SELECT * FROM " + "\nCROSS JOIN ".join(parts)

    def _order_by(self, query: Query, out_cols: list[OutputColumn]) -> list[str]:
        d = self.d
        by_name = {c.name.casefold(): c.name for c in out_cols}
        for c in out_cols:
            if c.source:
                by_name.setdefault(c.source.casefold(), c.name)
        order: list[str] = []
        for spec in query.order_by:
            text = str(spec).strip()
            direction = "ASC"
            low = text.lower()
            if low.endswith(" desc"):
                text, direction = text[:-5].strip(), "DESC"
            elif low.endswith(" asc"):
                text = text[:-4].strip()
            key = text.strip("[]").casefold() if text.startswith("[") else text.casefold()
            if key not in by_name:
                raise CompileError(f"order_by {spec!r} is not an output column")
            order.append(f"{d.quote(by_name[key])} {direction} NULLS LAST")
        if not order:
            order = [f"{d.quote(c.name)} ASC NULLS LAST" for c in out_cols if c.kind == "dimension"]
        return order


def _indent(text: str, n: int = 2) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in text.split("\n"))


def compile_query(
    model: Model,
    query: Query,
    relation_for: Callable[[Table], str] | None = None,
    allow_adhoc_measures: bool = False,
) -> CompiledQuery:
    return QueryCompiler(model, relation_for, allow_adhoc_measures=allow_adhoc_measures).compile(query)
