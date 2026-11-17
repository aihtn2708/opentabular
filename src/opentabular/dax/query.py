"""DAX queries: `[DEFINE MEASURE ...] EVALUATE <table> [ORDER BY ...]`.

Only the query shapes that map onto a semantic query are accepted:

    EVALUATE SUMMARIZECOLUMNS(<columns>, <filters>, "Name", <expression>, ...)
    EVALUATE ROW("Name", <expression>, ...)
    EVALUATE { <expression> }
    EVALUATE VALUES(Table[Column])            (also DISTINCT)
    EVALUATE TOPN(n, <one of the above>, <order expression> [, ASC | DESC])
    EVALUATE CALCULATETABLE(<one of the above>, <filters>)

A filter is anything CALCULATE accepts (a condition on a column, FILTER,
KEEPFILTERS) or TREATAS({values}, Table[Column]).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ast as A
from .parser import DaxSyntaxError, Parser


@dataclass
class DaxQuery:
    defines: list[tuple[str, str, A.Node]] = field(default_factory=list)  # (table, name, expr)
    group_by: list[A.ColumnRef] = field(default_factory=list)
    filters: list[A.Node] = field(default_factory=list)
    measures: list[tuple[str, A.Node]] = field(default_factory=list)
    order_by: list[tuple[A.Node, bool]] = field(default_factory=list)  # (expr, descending)
    limit: int | None = None


class DaxQueryError(Exception):
    pass


def _kw(p: Parser, word: str) -> bool:
    return p.tok.kind == "IDENT" and p.tok.text.upper() == word


def parse_dax_query(src: str) -> DaxQuery:
    p = Parser(src)
    q = DaxQuery()
    try:
        if _kw(p, "DEFINE"):
            p.i += 1
            while _kw(p, "MEASURE"):
                p.i += 1
                target = p._primary()
                if not isinstance(target, A.ColumnRef):
                    raise DaxQueryError("DEFINE MEASURE expects Table[Name] = <expression>")
                p._expect_op("=")
                q.defines.append((target.table, target.column, p.expression()))
            if not q.defines:
                raise DaxQueryError("DEFINE supports MEASURE definitions only")
        if not _kw(p, "EVALUATE"):
            raise DaxQueryError("a DAX query starts with EVALUATE (optionally after DEFINE MEASURE)")
        p.i += 1
        table = p.expression()
        if _kw(p, "ORDER"):
            p.i += 1
            if not _kw(p, "BY"):
                raise DaxQueryError("expected BY after ORDER")
            p.i += 1
            while True:
                expr = p.expression()
                desc = False
                if _kw(p, "ASC") or _kw(p, "DESC"):
                    desc = p.tok.text.upper() == "DESC"
                    p.i += 1
                q.order_by.append((expr, desc))
                if p._is_op(","):
                    p.i += 1
                    continue
                break
        if p.tok.kind != "EOF":
            raise DaxQueryError(f"unexpected '{p.tok.text}' after the query (one EVALUATE per query)")
    except DaxSyntaxError as e:
        raise DaxQueryError(str(e)) from None
    _table(table, q)
    return q


def _filter(node: A.Node) -> A.Node:
    if isinstance(node, A.Call) and node.name == "TREATAS":
        if len(node.args) == 2 and isinstance(node.args[0], A.SetLiteral) and isinstance(node.args[1], A.ColumnRef):
            return A.Binary("IN", node.args[1], node.args[0])
        raise DaxQueryError("TREATAS is supported as TREATAS({values}, Table[Column])")
    return node


def _table(node: A.Node, q: DaxQuery) -> None:
    if isinstance(node, A.SetLiteral):
        if len(node.items) != 1:
            raise DaxQueryError("a table constructor query must hold one expression: EVALUATE { [Measure] }")
        q.measures.append(("Value", node.items[0]))
        return
    if not isinstance(node, A.Call):
        raise DaxQueryError("EVALUATE needs a table expression such as SUMMARIZECOLUMNS(...)")
    name, args = node.name, list(node.args)
    if name == "SUMMARIZECOLUMNS":
        i = 0
        while i < len(args) and isinstance(args[i], A.ColumnRef):
            q.group_by.append(args[i])
            i += 1
        while i < len(args) and not isinstance(args[i], A.Str):
            q.filters.append(_filter(args[i]))
            i += 1
        rest = args[i:]
        if len(rest) % 2 or not all(isinstance(rest[k], A.Str) for k in range(0, len(rest), 2)):
            raise DaxQueryError('SUMMARIZECOLUMNS expects "Name", <expression> pairs after columns and filters')
        q.measures += [(rest[k].value, rest[k + 1]) for k in range(0, len(rest), 2)]
        if not q.group_by and not q.measures:
            raise DaxQueryError("SUMMARIZECOLUMNS needs at least one column or measure")
    elif name == "ROW":
        if not args or len(args) % 2 or not all(isinstance(args[k], A.Str) for k in range(0, len(args), 2)):
            raise DaxQueryError('ROW expects "Name", <expression> pairs')
        q.measures += [(args[k].value, args[k + 1]) for k in range(0, len(args), 2)]
    elif name in ("VALUES", "DISTINCT") and len(args) == 1 and isinstance(args[0], A.ColumnRef):
        q.group_by.append(args[0])
    elif name == "TOPN" and len(args) in (3, 4):
        if not isinstance(args[0], A.Num) or not args[0].text.isdigit():
            raise DaxQueryError("TOPN needs a literal number of rows")
        _table(args[1], q)
        q.limit = int(args[0].text)
        asc = len(args) == 4 and isinstance(args[3], A.NameRef) and args[3].name.upper() == "ASC"
        q.order_by.insert(0, (args[2], not asc))
    elif name == "CALCULATETABLE" and args:
        _table(args[0], q)
        q.filters += [_filter(a) for a in args[1:]]
    else:
        raise DaxQueryError(
            f"EVALUATE {name}(...) is not supported; use SUMMARIZECOLUMNS, ROW, VALUES, TOPN or CALCULATETABLE"
        )
