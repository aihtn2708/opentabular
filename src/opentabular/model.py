"""In-memory semantic model: tables, columns, measures and relationships.

The shape follows the Tabular Object Model that TMDL serialises, reduced to
the parts the compiler needs. Names are case-insensitive, as they are in DAX.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


class ModelError(Exception):
    """Raised when the model is inconsistent or a name cannot be resolved."""


def _key(name: str) -> str:
    return name.casefold()


@dataclass
class Column:
    name: str
    table: str
    data_type: str = "string"
    source_column: str | None = None
    expression: str | None = None  # calculated column (DAX), not compiled yet
    description: str | None = None
    format_string: str | None = None
    is_hidden: bool = False
    is_key: bool = False

    @property
    def physical_name(self) -> str:
        return self.source_column or self.name


@dataclass
class Measure:
    name: str
    table: str
    expression: str
    description: str | None = None
    format_string: str | None = None
    display_folder: str | None = None
    is_hidden: bool = False
    # TMDL `annotation Name = value` children. Two are given meaning here:
    # Owner (who answers for the definition) and Endorsement (Certified).
    annotations: dict[str, str] = field(default_factory=dict)

    @property
    def owner(self) -> str | None:
        return _annotation(self.annotations, "Owner")

    @property
    def endorsement(self) -> str | None:
        return _annotation(self.annotations, "Endorsement")

    @property
    def trust(self) -> str:
        """certified: endorsed by its owner. governed: defined in the model."""
        return "certified" if (self.endorsement or "").casefold() == "certified" else "governed"


def _annotation(annotations: dict[str, str], name: str) -> str | None:
    for k, v in annotations.items():
        if k.casefold() == name.casefold():
            return v
    return None


@dataclass
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)
    description: str | None = None
    is_hidden: bool = False
    # Physical binding, taken from an `entity` partition when present.
    entity_name: str | None = None
    schema_name: str | None = None
    partition_kind: str | None = None  # entity | m | calculated | ...
    partition_source: str | None = None  # raw M / DAX text, kept for reference

    def column(self, name: str) -> Column | None:
        return self.columns.get(_key(name))


@dataclass
class Relationship:
    name: str
    from_table: str  # many side
    from_column: str
    to_table: str  # one side
    to_column: str
    is_active: bool = True
    cross_filter: str = "oneDirection"
    from_cardinality: str = "many"
    to_cardinality: str = "one"


@dataclass
class Role:
    """A security role: a DAX row filter per table (row-level security)."""

    name: str
    table_filters: dict[str, str] = field(default_factory=dict)  # table name -> DAX
    members: list[str] = field(default_factory=list)


@dataclass
class Model:
    name: str = "Model"
    tables: dict[str, Table] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    roles: dict[str, Role] = field(default_factory=dict)
    # Objects found in the TMDL that the engine does not act on yet.
    unsupported: list[str] = field(default_factory=list)

    # -- lookup -----------------------------------------------------------
    def add_table(self, table: Table) -> None:
        self.tables[_key(table.name)] = table

    def table(self, name: str) -> Table:
        t = self.tables.get(_key(name))
        if t is None:
            raise ModelError(f"Unknown table '{name}'")
        return t

    def has_table(self, name: str) -> bool:
        return _key(name) in self.tables

    def column(self, table: str, column: str) -> Column:
        c = self.table(table).column(column)
        if c is None:
            raise ModelError(f"Unknown column '{table}'[{column}]")
        return c

    def find_measure(self, name: str) -> Measure | None:
        k = _key(name)
        for t in self.tables.values():
            if k in t.measures:
                return t.measures[k]
        return None

    def all_measures(self) -> list[Measure]:
        return [m for t in self.tables.values() for m in t.measures.values()]

    # -- relationship graph ----------------------------------------------
    def active_relationships(self, activated: frozenset[str] = frozenset()) -> list[Relationship]:
        """Relationships in force. `activated` names inactive relationships
        switched on by USERELATIONSHIP; each one replaces the other
        relationships between the same two tables."""
        on = [r for r in self.relationships if r.name in activated]
        pairs = {frozenset((_key(r.from_table), _key(r.to_table))) for r in on}
        for r in self.relationships:
            if r.is_active and r.name not in activated \
                    and frozenset((_key(r.from_table), _key(r.to_table))) not in pairs:
                on.append(r)
        return on

    def paths_from(self, table: str, activated: frozenset[str] = frozenset()) -> dict[str, list[Relationship]]:
        """Tables reachable from `table` by following active many-to-one
        relationships, with the relationship path to each (shortest first).

        This is the "expanded table" of DAX: a filter on any table in the
        result filters `table`.
        """
        start = _key(table)
        rels = self.active_relationships(activated)
        paths: dict[str, list[Relationship]] = {start: []}
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            for rel in rels:
                if _key(rel.from_table) != cur:
                    continue
                nxt = _key(rel.to_table)
                if nxt not in paths:
                    paths[nxt] = paths[cur] + [rel]
                    queue.append(nxt)
        return paths

    def key_column(self, table: str) -> Column | None:
        """The column that identifies a row of `table`: one marked isKey, or
        the column other tables relate to."""
        t = self.table(table)
        for c in t.columns.values():
            if c.is_key:
                return c
        for r in self.relationships:
            if _key(r.to_table) == _key(table):
                return t.column(r.to_column)
        return None

    def find_relationship(self, a: tuple[str, str], b: tuple[str, str]) -> Relationship | None:
        ka, kb = (_key(a[0]), _key(a[1])), (_key(b[0]), _key(b[1]))
        for r in self.relationships:
            ends = {(_key(r.from_table), _key(r.from_column)), (_key(r.to_table), _key(r.to_column))}
            if ends == {ka, kb}:
                return r
        return None

    def expanded(self, table: str) -> frozenset[str]:
        return frozenset(self.paths_from(table))

    # -- structural validation -------------------------------------------
    def structural_issues(self) -> list[str]:
        issues: list[str] = []
        seen: dict[str, str] = {}
        for t in self.tables.values():
            for m in t.measures.values():
                k = _key(m.name)
                if k in seen:
                    issues.append(
                        f"Measure [{m.name}] is defined in both '{seen[k]}' and '{t.name}'"
                    )
                seen[k] = t.name
                if t.column(m.name) is not None:
                    issues.append(f"'{t.name}' has a column and a measure both named [{m.name}]")
        for r in self.relationships:
            for tbl, col, side in (
                (r.from_table, r.from_column, "fromColumn"),
                (r.to_table, r.to_column, "toColumn"),
            ):
                if not self.has_table(tbl):
                    issues.append(f"Relationship {r.name}: {side} table '{tbl}' does not exist")
                elif self.table(tbl).column(col) is None:
                    issues.append(f"Relationship {r.name}: {side} '{tbl}'[{col}] does not exist")
        for role in self.roles.values():
            for tbl in role.table_filters:
                if not self.has_table(tbl):
                    issues.append(f"Role '{role.name}': table '{tbl}' does not exist")
        return issues
