"""Metric tests as code.

Tests live in YAML files (by default `tests/*.yml` in the project) and pin the
value of a semantic query, so a change to a measure or to the model that
alters a number fails in CI:

    tests:
      - name: revenue by year
        query:
          measures: [Total Sales]
          group_by: ["'Date'[Year]"]
        expect:
          rows:
            - {Year: 2023, Total Sales: 1250.0}
            - {Year: 2024, Total Sales: 1810.5}
      - name: three categories sold
        query: {measures: [Total Sales], group_by: ["Product[Category]"]}
        expect: {row_count: 3}
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from .compiler import CompileError, Query
from .project import Project
from .security import Identity


@dataclass
class Outcome:
    name: str
    file: str
    passed: bool
    detail: str = ""


def _same(expected, actual, tol: float) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None
    if isinstance(actual, Decimal):
        actual = float(actual)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(float(expected), float(actual), rel_tol=tol, abs_tol=tol)
    if isinstance(actual, (dt.date, dt.datetime)):
        return str(expected) == actual.isoformat() or expected == actual
    return str(expected) == str(actual)


def _query_from(spec: dict) -> Query:
    known = {"measures", "group_by", "filters", "order_by", "limit"}
    extra = set(spec) - known
    if extra:
        raise CompileError(f"unknown query key(s): {', '.join(sorted(extra))}")
    return Query(
        measures=list(spec.get("measures") or []),
        group_by=list(spec.get("group_by") or []),
        filters=list(spec.get("filters") or []),
        order_by=list(spec.get("order_by") or []),
        limit=spec.get("limit"),
    )


def run_test(project: Project, spec: dict, file: str) -> Outcome:
    name = str(spec.get("name", "(unnamed)"))
    try:
        identity = None
        if spec.get("roles") or spec.get("user"):  # run the test as a restricted viewer
            identity = Identity(spec.get("user"), list(spec.get("roles") or []))
        result = project.run(_query_from(spec.get("query") or {}), identity=identity)
    except Exception as e:  # noqa: BLE001 - any failure fails the test
        return Outcome(name, file, False, f"query failed: {e}")
    expect = spec.get("expect") or {}
    tol = float(expect.get("tolerance", 1e-6))
    records = result.records()
    if "row_count" in expect and len(records) != expect["row_count"]:
        return Outcome(name, file, False, f"expected {expect['row_count']} rows, got {len(records)}")
    if "rows" in expect:
        want = expect["rows"] or []
        if len(want) != len(records):
            return Outcome(name, file, False, f"expected {len(want)} rows, got {len(records)}: {records}")
        for i, (w, got) in enumerate(zip(want, records)):
            for key, value in w.items():
                if key not in got:
                    return Outcome(name, file, False, f"row {i + 1}: no column {key!r} in result")
                if not _same(value, got[key], tol):
                    return Outcome(
                        name, file, False, f"row {i + 1}, {key}: expected {value!r}, got {got[key]!r}"
                    )
    return Outcome(name, file, True)


def run_tests(project: Project, paths: list[Path] | None = None) -> list[Outcome]:
    if not paths:
        folder = project.root / str(project.config.get("tests", "tests"))
        paths = sorted(folder.glob("*.yml")) + sorted(folder.glob("*.yaml")) if folder.is_dir() else []
    outcomes: list[Outcome] = []
    for path in paths:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for spec in doc.get("tests") or []:
            outcomes.append(run_test(project, spec, str(path)))
    return outcomes
