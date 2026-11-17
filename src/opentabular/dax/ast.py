"""DAX abstract syntax tree. Nodes are frozen so they can be hashed and
deduplicated by the compiler."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    pass


@dataclass(frozen=True)
class Num(Node):
    text: str


@dataclass(frozen=True)
class Str(Node):
    value: str


@dataclass(frozen=True)
class Bool(Node):
    value: bool


@dataclass(frozen=True)
class ColumnRef(Node):
    """`Table[Column]` or `'Table Name'[Column]`. May also name a measure."""

    table: str
    column: str


@dataclass(frozen=True)
class BareRef(Node):
    """`[Name]`: a measure, or a column of the current row context."""

    name: str


@dataclass(frozen=True)
class NameRef(Node):
    """An unquoted identifier: a variable or a table name."""

    name: str


@dataclass(frozen=True)
class TableRef(Node):
    """A quoted table name used on its own: `'Sales'`."""

    name: str


@dataclass(frozen=True)
class Call(Node):
    name: str  # upper-cased
    args: tuple[Node, ...]


@dataclass(frozen=True)
class Unary(Node):
    op: str  # '-', '+', 'NOT'
    operand: Node


@dataclass(frozen=True)
class Binary(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class SetLiteral(Node):
    """`{1, 2, 3}`: a one-column table constructor, used with IN."""

    items: tuple[Node, ...]


@dataclass(frozen=True)
class VarBlock(Node):
    vars: tuple[tuple[str, Node], ...]
    body: Node


def walk(node: Node):
    """Yield `node` and every node beneath it."""
    yield node
    if isinstance(node, Call):
        for a in node.args:
            yield from walk(a)
    elif isinstance(node, Unary):
        yield from walk(node.operand)
    elif isinstance(node, Binary):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, SetLiteral):
        for a in node.items:
            yield from walk(a)
    elif isinstance(node, VarBlock):
        for _, e in node.vars:
            yield from walk(e)
        yield from walk(node.body)
