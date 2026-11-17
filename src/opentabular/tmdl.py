"""TMDL (Tabular Model Definition Language) reader.

Two layers:

1. `parse_tmdl(text)` turns TMDL text into a generic tree of `Node`s. It
   understands the grammar (indentation, `name: value` properties, `= expr`
   default properties, multi-line and ``` fenced expressions, `///`
   descriptions, `ref` lines) but not the meaning of any object type, so
   unknown objects survive parsing.
2. `load_model(path)` reads a TMDL folder (or a single file) and builds a
   `Model` from the table, column, measure, partition and relationship nodes.

Grammar reference: https://learn.microsoft.com/analysis-services/tmdl/tmdl-overview
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .model import Column, Measure, Model, Relationship, Table


class TmdlError(Exception):
    def __init__(self, message: str, file: str | None = None, line: int | None = None):
        where = ""
        if file:
            where = f"{file}:{line}: " if line else f"{file}: "
        super().__init__(where + message)
        self.file, self.line = file, line


@dataclass
class Node:
    type: str
    name: str | None = None
    expr: str | None = None  # default property / expression after `=`
    props: dict[str, object] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    description: str | None = None
    line: int = 0

    def child(self, type_: str) -> "Node | None":
        for c in self.children:
            if c.type.lower() == type_.lower():
                return c
        return None

    def prop(self, name: str, default=None):
        for k, v in self.props.items():
            if k.lower() == name.lower():
                return v
        return default


@dataclass
class _Line:
    no: int
    level: int
    text: str  # stripped of indentation and trailing whitespace
    raw: str

    @property
    def blank(self) -> bool:
        return self.text == ""


_PROP = re.compile(r"^([A-Za-z_][\w.]*)\s*:(.*)$")
_ASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*=(.*)$")
_BARE = re.compile(r"^[A-Za-z_]\w*$")
_OBJECT = re.compile(r"^([A-Za-z_]\w*)\s+(.*)$")


def _indent_unit(lines: list[str]) -> str:
    for ln in lines:
        if ln.startswith("\t"):
            return "\t"
        m = re.match(r"^( +)\S", ln)
        if m:
            return m.group(1)
    return "\t"


def _split_lines(text: str) -> tuple[list[_Line], str]:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unit = _indent_unit(raw_lines)
    out = []
    for i, raw in enumerate(raw_lines, 1):
        stripped = raw.strip()
        if not stripped:
            out.append(_Line(i, -1, "", raw))
            continue
        lead = raw[: len(raw) - len(raw.lstrip())]
        level = lead.count("\t") if unit == "\t" else len(lead.replace("\t", unit)) // len(unit)
        out.append(_Line(i, level, stripped, raw))
    return out, unit


def _read_name(rest: str) -> tuple[str, str]:
    """Read an object name from the start of `rest`; return (name, remainder)."""
    rest = rest.lstrip()
    if rest.startswith("'"):
        i, buf = 1, []
        while i < len(rest):
            if rest[i] == "'":
                if rest[i + 1 : i + 2] == "'":
                    buf.append("'")
                    i += 2
                    continue
                return "".join(buf), rest[i + 1 :]
            buf.append(rest[i])
            i += 1
        raise ValueError("unterminated quoted name")
    m = re.match(r"^([^\s=]+)(.*)$", rest)
    if not m:
        return "", rest
    return m.group(1), m.group(2)


def _unquote_value(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('""', '"')
    return v


def _dedent(raw_lines: list[str], n_chars: int) -> str:
    out = []
    for ln in raw_lines:
        if not ln.strip():
            out.append("")
        else:
            lead = len(ln) - len(ln.lstrip())
            out.append(ln[min(n_chars, lead) :].rstrip())
    while out and out[-1] == "":
        out.pop()
    while out and out[0] == "":
        out.pop(0)
    return "\n".join(out)


class _Parser:
    def __init__(self, text: str, file: str | None = None):
        self.lines, self.unit = _split_lines(text)
        self.file = file
        self.i = 0

    def err(self, msg: str, line: _Line) -> TmdlError:
        return TmdlError(msg, self.file, line.no)

    # -- expressions -----------------------------------------------------
    def _read_expression(self, rest: str, min_level: int, decl: _Line) -> str:
        """Read the expression that follows `=` on the line just consumed."""
        rest = rest.strip()
        if rest.startswith("```"):
            body = rest[3:]
            if body.rstrip().endswith("```") and body.strip() != "":
                return body.rstrip()[:-3].strip()
            raw: list[str] = [body] if body.strip() else []
            while self.i < len(self.lines):
                ln = self.lines[self.i]
                self.i += 1
                if ln.text == "```":
                    boundary = len(ln.raw) - len(ln.raw.lstrip())
                    return _dedent(raw, boundary)
                raw.append(ln.raw)
            raise self.err("unterminated ``` expression block", decl)
        raw = []
        if rest:
            # Single-line form. Be lenient and also accept continuation lines
            # indented at expression depth, which some tools emit.
            cont: list[str] = []
            while self.i < len(self.lines):
                ln = self.lines[self.i]
                if ln.blank or ln.level < min_level:
                    break
                cont.append(ln.text)
                self.i += 1
            return "\n".join([rest] + cont)
        while self.i < len(self.lines):
            ln = self.lines[self.i]
            if not ln.blank and ln.level < min_level:
                break
            raw.append(ln.raw)
            self.i += 1
        # Trailing blank lines belong to whatever follows, not the expression.
        while raw and not raw[-1].strip():
            raw.pop()
            self.i -= 1
        # Strip the indentation the lines share. Power BI writes some
        # expressions (a partition's M source) deeper than the minimum.
        common = min(
            (len(r) - len(r.lstrip()) for r in raw if r.strip()),
            default=len(self.unit) * min_level,
        )
        return _dedent(raw, common)

    # -- objects ---------------------------------------------------------
    def parse_file(self) -> list[Node]:
        nodes: list[Node] = []
        while self.i < len(self.lines):
            ln = self.lines[self.i]
            if ln.blank:
                self.i += 1
                continue
            if ln.level != 0:
                raise self.err("unexpected indentation at top level", ln)
            node = self._parse_object(0)
            if node is not None:
                nodes.append(node)
        return nodes

    def _parse_object(self, level: int) -> Node | None:
        desc: list[str] = []
        while self.i < len(self.lines) and self.lines[self.i].text.startswith("///"):
            desc.append(self.lines[self.i].text[3:].strip())
            self.i += 1
        if self.i >= len(self.lines):
            return None
        decl = self.lines[self.i]
        self.i += 1
        text = decl.text

        if text.lower().startswith("ref "):
            return None  # ordering hint only
        if text.lower() == "createorreplace":
            # TMDL script wrapper: its children are ordinary top-level objects.
            wrapper = Node("createOrReplace", line=decl.no)
            self._parse_body(wrapper, level)
            return wrapper

        m = _OBJECT.match(text)
        if not m:
            # A bare word with an indented body, e.g. `source` under an
            # entity partition.
            node = Node(text, line=decl.no)
            self._parse_body(node, level)
            return node
        node = Node(m.group(1), line=decl.no, description="\n".join(desc) or None)
        try:
            node.name, rest = _read_name(m.group(2))
        except ValueError as e:
            raise self.err(str(e), decl) from None
        rest = rest.strip()
        if rest.startswith("="):
            node.expr = self._read_expression(rest[1:], level + 2, decl)
        elif rest:
            raise self.err(f"unexpected text after object name: {rest!r}", decl)
        self._parse_body(node, level)
        return node

    def _parse_body(self, node: Node, level: int) -> None:
        body = level + 1
        while self.i < len(self.lines):
            ln = self.lines[self.i]
            if ln.blank:
                self.i += 1
                continue
            if ln.level < body:
                return
            if ln.level > body:
                raise self.err("unexpected indentation", ln)
            text = ln.text
            if text.startswith("///"):
                child = self._parse_object(body)
                if child is not None:
                    node.children.append(child)
                continue
            pm = _PROP.match(text)
            if pm:
                node.props[pm.group(1)] = _unquote_value(pm.group(2) or "")
                self.i += 1
                continue
            am = _ASSIGN.match(text)
            if am:
                self.i += 1
                node.props[am.group(1)] = self._read_expression(am.group(2), body + 1, ln)
                continue
            if _BARE.match(text):
                nxt = self._peek_level(self.i + 1)
                if nxt is not None and nxt > body:
                    child = self._parse_object(body)
                    if child is not None:
                        node.children.append(child)
                else:
                    node.props[text] = True
                    self.i += 1
                continue
            child = self._parse_object(body)
            if child is not None:
                node.children.append(child)

    def _peek_level(self, idx: int) -> int | None:
        while idx < len(self.lines):
            if not self.lines[idx].blank:
                return self.lines[idx].level
            idx += 1
        return None


def parse_tmdl(text: str, file: str | None = None) -> list[Node]:
    """Parse TMDL text into a list of top-level nodes."""
    nodes = _Parser(text, file).parse_file()
    flat: list[Node] = []
    for n in nodes:
        flat.extend(n.children if n.type == "createOrReplace" else [n])
    return flat


# ---------------------------------------------------------------------------
# Nodes -> Model
# ---------------------------------------------------------------------------

def _as_bool(v, default=False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def _split_column_ref(ref: str) -> tuple[str, str]:
    """Split `Table.Column` where either part may be single-quoted."""
    ref = ref.strip()
    if ref.startswith("'"):
        table, rest = _read_name(ref)
    else:
        table, dot, tail = ref.partition(".")
        rest = dot + tail
    if not table or not rest.startswith(".") or len(rest) < 2:
        raise ValueError(f"expected Table.Column, got {ref!r}")
    rest = rest[1:]
    if rest.startswith("'"):
        column, tail = _read_name(rest)
        if tail.strip():
            raise ValueError(f"expected Table.Column, got {ref!r}")
    else:
        column = rest
    return table, column


_IGNORED_TABLE_CHILDREN = {"annotation", "changedproperty", "extendedproperty", "variation"}
_UNSUPPORTED_TABLE_CHILDREN = {"calculationgroup", "calculationitem"}
_UNSUPPORTED_TOP = {"role", "function", "perspective"}


def build_model(nodes: list[Node], file: str | None = None) -> Model:
    model = Model()
    for n in nodes:
        kind = n.type.lower()
        if kind == "model":
            model.name = n.name or model.name
        elif kind == "database":
            pass
        elif kind == "table":
            _add_table(model, n)
        elif kind == "relationship":
            try:
                ft, fc = _split_column_ref(str(n.prop("fromColumn", "")))
                tt, tc = _split_column_ref(str(n.prop("toColumn", "")))
            except ValueError as e:
                raise TmdlError(f"relationship {n.name}: {e}", file, n.line) from None
            model.relationships.append(
                Relationship(
                    name=n.name or "",
                    from_table=ft,
                    from_column=fc,
                    to_table=tt,
                    to_column=tc,
                    is_active=_as_bool(n.prop("isActive"), True),
                    cross_filter=str(n.prop("crossFilteringBehavior", "oneDirection")),
                    from_cardinality=str(n.prop("fromCardinality", "many")),
                    to_cardinality=str(n.prop("toCardinality", "one")),
                )
            )
        elif kind in _UNSUPPORTED_TOP:
            model.unsupported.append(f"{n.type} '{n.name}'")
    return model


def _add_table(model: Model, n: Node) -> None:
    name = n.name or ""
    if model.has_table(name):
        table = model.table(name)  # partial declaration in another file
    else:
        table = Table(name=name)
        model.add_table(table)
    table.description = n.description or table.description
    table.is_hidden = _as_bool(n.prop("isHidden"), table.is_hidden)
    for c in n.children:
        kind = c.type.lower()
        if kind == "column":
            col = Column(
                name=c.name or "",
                table=name,
                data_type=str(c.prop("dataType", "string")),
                source_column=(str(c.prop("sourceColumn")) if c.prop("sourceColumn") else None),
                expression=c.expr,
                description=c.description,
                format_string=(str(c.prop("formatString")) if c.prop("formatString") else None),
                is_hidden=_as_bool(c.prop("isHidden")),
            )
            table.columns[col.name.casefold()] = col
        elif kind == "measure":
            if not c.expr:
                raise TmdlError(f"measure '{c.name}' has no expression", None, c.line)
            m = Measure(
                name=c.name or "",
                table=name,
                expression=c.expr,
                description=c.description,
                format_string=(str(c.prop("formatString")) if c.prop("formatString") else None),
                display_folder=(str(c.prop("displayFolder")) if c.prop("displayFolder") else None),
                is_hidden=_as_bool(c.prop("isHidden")),
                annotations={
                    a.name: (a.expr or "") for a in c.children
                    if a.type.lower() == "annotation" and a.name
                },
            )
            table.measures[m.name.casefold()] = m
        elif kind == "partition":
            table.partition_kind = (c.expr or "").strip().lower() or None
            src = c.child("source")
            if src is not None:  # entity partition
                table.entity_name = str(src.prop("entityName")) if src.prop("entityName") else None
                table.schema_name = str(src.prop("schemaName")) if src.prop("schemaName") else None
            elif isinstance(c.prop("source"), str):
                table.partition_source = str(c.prop("source"))
                if table.partition_kind == "calculated":
                    model.unsupported.append(f"calculated table '{name}'")
        elif kind in _UNSUPPORTED_TABLE_CHILDREN:
            model.unsupported.append(f"{c.type} in table '{name}'")
        elif kind in _IGNORED_TABLE_CHILDREN or kind in {"hierarchy"}:
            continue


def load_model(path: str | Path) -> Model:
    """Load a model from a TMDL folder (as written by Power BI / Fabric) or
    from a single .tmdl file."""
    path = Path(path)
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(path.rglob("*.tmdl"))
    else:
        raise TmdlError(f"no such file or folder: {path}")
    if not files:
        raise TmdlError(f"no .tmdl files found under {path}")
    nodes: list[Node] = []
    for f in files:
        nodes.extend(parse_tmdl(f.read_text(encoding="utf-8-sig"), str(f)))
    return build_model(nodes)
