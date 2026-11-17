"""Lineage: which measures, columns and tables a measure is built from.

Derived from the DAX syntax tree, so it needs no database and stays correct
for measures the compiler cannot translate yet.
"""
from __future__ import annotations

from .dax import DaxSyntaxError, parse_dax
from .dax import ast as A
from .model import Measure, Model

_ROW_FUNCS = {"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "FILTER", "RANKX", "ADDCOLUMNS", "TOPN"}


def measure_lineage(model: Model, measure: Measure) -> dict:
    """Return {"direct_measures", "measures", "columns", "tables", "unresolved"}.

    `measures` is every measure reached, directly or through other measures;
    `columns` and `tables` include those used by the measures reached.
    """
    direct: list[str] = []
    measures: list[str] = []
    columns: list[str] = []
    tables: list[str] = []
    unresolved: list[str] = []

    def add(lst: list[str], item: str) -> None:
        if item not in lst:
            lst.append(item)

    def add_column(table: str, column: str) -> None:
        t = model.table(table)
        c = t.column(column)
        add(columns, f"'{t.name}'[{c.name}]")
        add(tables, t.name)

    def visit_measure(m: Measure, depth: int) -> None:
        try:
            node = parse_dax(m.expression)
        except DaxSyntaxError:
            add(unresolved, f"[{m.name}] (syntax error)")
            return
        visit(node, None, depth)

    def reference_measure(target: Measure, depth: int) -> None:
        if depth == 0:
            add(direct, target.name)
        if target.name not in measures and target.name != measure.name:
            measures.append(target.name)
            visit_measure(target, depth + 1)

    def visit(node: A.Node, row_table: str | None, depth: int) -> None:
        if isinstance(node, A.BareRef):
            target = model.find_measure(node.name)
            if target is not None:
                reference_measure(target, depth)
            elif row_table and model.table(row_table).column(node.name) is not None:
                add_column(row_table, node.name)
            else:
                add(unresolved, f"[{node.name}]")
        elif isinstance(node, A.ColumnRef):
            if model.has_table(node.table) and model.table(node.table).column(node.column) is not None:
                add_column(node.table, node.column)
            elif model.find_measure(node.column) is not None:
                reference_measure(model.find_measure(node.column), depth)
            else:
                add(unresolved, f"'{node.table}'[{node.column}]")
        elif isinstance(node, (A.NameRef, A.TableRef)):
            if model.has_table(node.name):
                add(tables, model.table(node.name).name)
        elif isinstance(node, A.Call):
            inner = row_table
            if node.name in _ROW_FUNCS and node.args:
                first = node.args[0]
                while isinstance(first, A.Call) and first.args and first.name in ("FILTER", "ALL", "VALUES", "DISTINCT"):
                    first = first.args[0]
                if isinstance(first, (A.NameRef, A.TableRef)) and model.has_table(first.name):
                    inner = first.name
            for i, a in enumerate(node.args):
                visit(a, inner if i > 0 else row_table, depth)
        elif isinstance(node, A.Unary):
            visit(node.operand, row_table, depth)
        elif isinstance(node, A.Binary):
            visit(node.left, row_table, depth)
            visit(node.right, row_table, depth)
        elif isinstance(node, A.SetLiteral):
            for a in node.items:
                visit(a, row_table, depth)
        elif isinstance(node, A.VarBlock):
            for _, e in node.vars:
                visit(e, row_table, depth)
            visit(node.body, row_table, depth)

    visit_measure(measure, 0)
    return {"direct_measures": direct, "measures": measures, "columns": columns,
            "tables": tables, "unresolved": unresolved}


def used_by(model: Model, measure: Measure) -> list[str]:
    """Measures whose definition reaches `measure`."""
    out = []
    for other in model.all_measures():
        if other.name != measure.name and measure.name in measure_lineage(model, other)["measures"]:
            out.append(other.name)
    return out
