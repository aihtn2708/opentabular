"""Command line interface: `otab`."""
from __future__ import annotations

import json
import sys

import click

from . import __version__
from .compiler import CompileError, Query, SUPPORTED_FUNCTIONS
from .project import Project, ProjectError
from .tmdl import TmdlError


def _load(path: str) -> Project:
    try:
        return Project.load(path)
    except (ProjectError, TmdlError) as e:
        raise click.ClickException(str(e)) from None


def _table(columns: list[str], rows: list[tuple]) -> str:
    def fmt(v) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:,.4f}".rstrip("0").rstrip(".")
        return str(v)

    cells = [[fmt(v) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(columns)]
    numeric = [bool(rows) and all(isinstance(r[i], (int, float)) or r[i] is None for r in rows)
               for i in range(len(columns))]
    line = lambda vals: "  ".join(  # noqa: E731
        v.rjust(w) if num else v.ljust(w) for v, w, num in zip(vals, widths, numeric)
    ).rstrip()
    out = [line(columns), line(["-" * w for w in widths])]
    out += [line(r) for r in cells]
    return "\n".join(out)


project_option = click.option(
    "-p", "--project", "project_path", default=".", show_default=True,
    help="Project folder (with opentabular.yml), or a TMDL folder.",
)
query_options = [
    click.option("-m", "--measure", "measures", multiple=True,
                 help='Measure name, or an ad-hoc one as "Name = <DAX>". Repeatable.'),
    click.option("-g", "--group-by", "group_by", multiple=True, help="Column as Table[Column]. Repeatable."),
    click.option("-f", "--filter", "filters", multiple=True,
                 help="DAX condition on one column, e.g. \"Product[Color] = \\\"Red\\\"\". Repeatable."),
    click.option("-o", "--order-by", "order_by", multiple=True, help='Output column, optionally " desc".'),
    click.option("-n", "--limit", type=int, default=None, help="Maximum number of rows."),
]


def _with_query_options(fn):
    for opt in reversed(query_options):
        fn = opt(fn)
    return fn


def _query(measures, group_by, filters, order_by, limit) -> Query:
    return Query(list(measures), list(group_by), list(filters), list(order_by), limit)


@click.group()
@click.version_option(__version__, prog_name="otab")
def main() -> None:
    """OpenTabular: a semantic layer on TMDL and DAX."""


@main.command()
@project_option
@click.option("--check-sources", is_flag=True, help="Also check bound columns against the database.")
@click.option("--strict", is_flag=True, help="Fail on unsupported DAX and warnings too.")
def validate(project_path: str, check_sources: bool, strict: bool) -> None:
    """Parse the model, compile every measure and report problems."""
    project = _load(project_path)
    try:
        issues = project.validate(check_sources=check_sources)
    except ProjectError as e:
        raise click.ClickException(str(e)) from None
    order = {"error": 0, "unsupported": 1, "warning": 2, "info": 3}
    for i in sorted(issues, key=lambda i: order[i.level]):
        click.echo(f"{i.level:<12}{i.where}: {i.message}")
    n_tables = len(project.model.tables)
    n_measures = len(project.model.all_measures())
    counts = {lvl: sum(1 for i in issues if i.level == lvl) for lvl in order}
    click.echo(
        f"{n_tables} tables, {n_measures} measures, {len(project.model.relationships)} relationships: "
        f"{counts['error']} errors, {counts['unsupported']} unsupported, "
        f"{counts['warning']} warnings, {counts['info']} notes"
    )
    failing = counts["error"] + (counts["unsupported"] + counts["warning"] if strict else 0)
    sys.exit(1 if failing else 0)


@main.command()
@project_option
@click.option("--json", "as_json", is_flag=True, help="Print the catalogue as JSON.")
def describe(project_path: str, as_json: bool) -> None:
    """Show the model catalogue: tables, columns, measures."""
    project = _load(project_path)
    info = project.describe()
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return
    click.echo(f"Model: {info['model']}")
    click.echo("\nMeasures")
    for m in info["measures"]:
        desc = f"  {m['description']}" if m["description"] else ""
        click.echo(f"  [{m['name']}]{desc}")
    click.echo("\nTables")
    for t in info["tables"]:
        click.echo(f"  {t['name']}: " + ", ".join(c["name"] for c in t["columns"]))
    click.echo("\nRelationships")
    for r in info["relationships"]:
        click.echo(f"  {r['from']} -> {r['to']}" + ("" if r["active"] else "  (inactive)"))


@main.command()
@project_option
@_with_query_options
@click.option("--dialect", default="duckdb", show_default=True,
              help="Experimental: translate the SQL for another database (postgres, snowflake, "
                   "bigquery, databricks, redshift, clickhouse, mysql). Needs the 'dialects' extra.")
def sql(project_path, measures, group_by, filters, order_by, limit, dialect) -> None:
    """Print the SQL a semantic query compiles to."""
    from .dialects import DialectError, transpile

    project = _load(project_path)
    try:
        compiled = project.compile(_query(measures, group_by, filters, order_by, limit), True)
        click.echo(transpile(compiled.sql, dialect))
    except (CompileError, DialectError) as e:
        raise click.ClickException(str(e)) from None


@main.command()
@click.argument("directory", default=".")
def init(directory: str) -> None:
    """Create a starter project: model, data, a metric test and an app."""
    from pathlib import Path

    from .scaffold import init_project

    try:
        written = init_project(Path(directory))
    except FileExistsError as e:
        raise click.ClickException(str(e)) from None
    for path in written:
        click.echo(f"created {path}")
    click.echo("Next: otab validate --check-sources, otab test, otab serve"
               + ("" if directory == "." else f"  (with -p {directory})"))


@main.group()
def agent() -> None:
    """Set up coding agents to work on this project."""


@agent.command("setup")
@project_option
def agent_setup_cmd(project_path: str) -> None:
    """Write AGENTS.md and agent skills for modelling and building apps."""
    from .scaffold import agent_setup

    project = _load(project_path)
    for path in agent_setup(project.root):
        click.echo(f"wrote {path}")


@main.command()
@project_option
@_with_query_options
@click.option("--json", "as_json", is_flag=True, help="Print rows as JSON.")
def query(project_path, measures, group_by, filters, order_by, limit, as_json) -> None:
    """Run a semantic query and print the result."""
    project = _load(project_path)
    try:
        result = project.run(_query(measures, group_by, filters, order_by, limit), True)
    except (CompileError, ProjectError) as e:
        raise click.ClickException(str(e)) from None
    if as_json:
        click.echo(json.dumps(result.records(), indent=2, default=str))
    else:
        click.echo(_table(result.columns, result.rows))


@main.command()
@project_option
@click.argument("files", nargs=-1, type=click.Path(exists=True))
def test(project_path: str, files: tuple[str, ...]) -> None:
    """Run the project's metric tests (tests/*.yml)."""
    from pathlib import Path

    from .testing import run_tests

    project = _load(project_path)
    try:
        outcomes = run_tests(project, [Path(f) for f in files] or None)
    except ProjectError as e:
        raise click.ClickException(str(e)) from None
    for o in outcomes:
        click.echo(f"{'PASS' if o.passed else 'FAIL'}  {o.name}" + (f"\n      {o.detail}" if o.detail else ""))
    failed = sum(1 for o in outcomes if not o.passed)
    click.echo(f"{len(outcomes) - failed} passed, {failed} failed")
    sys.exit(1 if failed or not outcomes else 0)


@main.command()
@project_option
def mcp(project_path: str) -> None:
    """Serve the model to AI clients over MCP (stdio)."""
    from .mcp_server import serve

    project = _load(project_path)
    try:
        serve(project)
    except ProjectError as e:
        raise click.ClickException(str(e)) from None


@main.command()
@project_option
@click.option("--host", default="127.0.0.1", show_default=True, help="Address to bind.")
@click.option("--port", default=7979, show_default=True, envvar="PORT", help="Port (or the PORT variable).")
@click.option("--allow-adhoc", is_flag=True, help="Let pages send ad-hoc DAX measures (shown as 'ad hoc').")
@click.option("--user", default="otab", show_default=True, envvar="OTAB_USER", help="User name for sign-in.")
@click.option("--password", default=None, envvar="OTAB_PASSWORD",
              help="Require this shared password (HTTP Basic). Also read from OTAB_PASSWORD.")
def serve(project_path: str, host: str, port: int, allow_adhoc: bool, user: str, password: str | None) -> None:
    """Serve the metrics explorer, the data apps and the HTTP API."""
    from .server import make_server

    if host not in ("127.0.0.1", "localhost", "::1") and not password:
        raise click.ClickException(
            "refusing to listen on a public address without a password: "
            "set OTAB_PASSWORD or pass --password"
        )
    try:
        server = make_server(project_path, host, port, allow_adhoc, user, password)
    except (ProjectError, TmdlError) as e:
        raise click.ClickException(str(e)) from None
    click.echo("Sign-in: " + (f"user '{user}' with the shared password" if password else "none (localhost only)"))
    click.echo(f"Explorer:  http://{host}:{port}/")
    for app in server.holder.apps():
        click.echo(f"App:       http://{host}:{port}/apps/{app}")
    click.echo("The model reloads when a .tmdl or .yml file changes. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@main.command()
@project_option
@click.argument("measure")
@click.option("--json", "as_json", is_flag=True, help="Print as JSON.")
def lineage(project_path: str, measure: str, as_json: bool) -> None:
    """Show what a measure is built from and what depends on it."""
    project = _load(project_path)
    info = project.explain(measure)
    if info is None:
        raise click.ClickException(f"unknown measure {measure!r}")
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return
    lin = info["lineage"]
    click.echo(f"[{info['name']}]  trust: {info['trust']}" + (f"  owner: {info['owner']}" if info["owner"] else ""))
    if info["description"]:
        click.echo(f"  {info['description']}")
    click.echo("\nDAX")
    for line in info["dax"].splitlines():
        click.echo(f"  {line}")
    click.echo("\nBuilt from measures: " + (", ".join(f"[{m}]" for m in lin["measures"]) or "none"))
    click.echo("Columns")
    for src in lin["sources"]:
        for col, physical in src["columns"].items():
            click.echo(f"  {col}  <-  {src['relation']}.{physical}")
    click.echo("Used by: " + (", ".join(f"[{m}]" for m in lin["used_by"]) or "none"))


@main.command()
@project_option
@click.argument("text")
def search(project_path: str, text: str) -> None:
    """Find measures and columns by name or description."""
    project = _load(project_path)
    hits = project.search(text)
    for h in hits:
        label = f"[{h['name']}]" if h["kind"] == "measure" else h["name"]
        click.echo(f"{h['kind']:<8} {label}" + (f"  {h['description']}" if h.get("description") else ""))
    if not hits:
        click.echo("no matches")


@main.command()
def functions() -> None:
    """List the DAX functions the compiler supports."""
    click.echo(", ".join(SUPPORTED_FUNCTIONS))


if __name__ == "__main__":
    main()
