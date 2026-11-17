"""`otab serve`: a local HTTP server for people and data apps.

    GET  /                     metrics explorer (built in)
    GET  /sdk.js               the JavaScript client used by data apps
    GET  /apps/<name>          a data app: <project>/apps/<name>.html
    GET  /api/model            catalogue: tables, columns, measures, apps
    POST /api/query            run a semantic query (JSON body)
    GET  /api/measures/<name>  definition, owner, trust level and lineage
    GET  /api/search?q=...     find measures and columns

Pages never send SQL or DAX. They ask for measures by name, and the response
carries the SQL that ran and the trust level of each measure, so every number
on a page can show where it came from.

The model is reloaded when a .tmdl or .yml file changes.

Access: there are no user accounts. Bind to localhost (the default), or set a
shared password (`--password` or the OTAB_PASSWORD environment variable) to
require HTTP Basic sign-in on every route except /healthz. A shared password
is enough for a test deployment; it is not per-user access control.
"""
from __future__ import annotations

import base64
import datetime as dt
import hmac
import json
import re
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .compiler import CompileError, Query
from .project import Project, ProjectError
from .tmdl import TmdlError

MAX_ROWS = 5000
_APP_NAME = re.compile(r"^[\w-]+$")


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    return v


def _asset(name: str) -> bytes:
    return resources.files("opentabular").joinpath("web", name).read_bytes()


class ProjectHolder:
    """Holds the loaded project and reloads it when its files change."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.project = Project.load(self.path)
        self.signature = self._signature()

    def _signature(self) -> tuple:
        root = self.project.root
        files = list(root.rglob("*.tmdl")) + list(root.glob("*.yml")) + list(root.glob("*.yaml"))
        return tuple(sorted((str(f), f.stat().st_mtime_ns) for f in files if f.is_file()))

    def get(self) -> Project:
        """Call with the lock held."""
        sig = self._signature()
        if sig != self.signature:
            self.project = Project.load(self.path)  # raises if the edit broke the model
            self.signature = sig
        return self.project

    def apps(self) -> list[str]:
        folder = self.project.root / "apps"
        return sorted(p.stem for p in folder.glob("*.html")) if folder.is_dir() else []


def run_query(project: Project, body: dict, allow_adhoc: bool) -> dict:
    def pick(*names: str, default=None):
        for n in names:
            if n in body and body[n] is not None:
                return body[n]
        return default

    limit = pick("limit")
    limit = MAX_ROWS if limit is None else max(0, min(int(limit), MAX_ROWS))
    q = Query(
        measures=list(pick("measures", default=[])),
        group_by=list(pick("group_by", "groupBy", default=[])),
        filters=list(pick("filters", default=[])),
        order_by=list(pick("order_by", "orderBy", default=[])),
        limit=limit + 1,
    )
    started = time.perf_counter()
    result = project.run(q, allow_adhoc_measures=allow_adhoc)
    return {
        "columns": [
            {"name": c.name, "kind": c.kind, "format": c.format_string, "source": c.source,
             "trust": c.trust, "owner": c.owner}
            for c in result.meta
        ],
        "rows": [[_jsonable(v) for v in r] for r in result.rows[:limit]],
        "truncated": len(result.rows) > limit,
        "sql": result.sql,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def make_handler(holder: ProjectHolder, allow_adhoc: bool, user: str = "otab", password: str | None = None):
    expected = None if not password else base64.b64encode(f"{user}:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "opentabular"

        def _authorised(self) -> bool:
            if expected is None:
                return True
            scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
            if scheme.lower() == "basic" and hmac.compare_digest(token.strip(), expected):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="OpenTabular", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def log_message(self, fmt: str, *args) -> None:  # quiet by default
            pass

        # -- helpers -----------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

        def _guarded(self, fn) -> None:
            """Run `fn(project)` under the lock; turn failures into JSON errors."""
            try:
                with holder.lock:
                    self._json(fn(holder.get()))
            except CompileError as e:
                self._json({"error": str(e), "kind": type(e).__name__}, 400)
            except (ProjectError, TmdlError) as e:
                self._json({"error": str(e), "kind": "model"}, 500)
            except (ValueError, TypeError, KeyError) as e:
                self._json({"error": f"bad request: {e}", "kind": "request"}, 400)

        # -- routes ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            path = unquote(url.path)
            if path == "/healthz":  # for the host's health check; reveals nothing
                return self._json({"ok": True})
            if not self._authorised():
                return
            if path in ("/", "/index.html"):
                return self._send(200, _asset("explorer.html"), "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/sdk.js":
                return self._send(200, _asset("opentabular.js"), "text/javascript; charset=utf-8")
            if path == "/api/model":
                return self._guarded(lambda p: {
                    **p.describe(), "apps": holder.apps(), "allow_adhoc": allow_adhoc,
                })
            if path.startswith("/api/measures/"):
                name = path[len("/api/measures/"):]

                def explain(p: Project) -> dict:
                    info = p.explain(name)
                    if info is None:
                        raise CompileError(f"unknown measure {name!r}")
                    return info

                return self._guarded(explain)
            if path == "/api/search":
                text = (parse_qs(url.query).get("q") or [""])[0]
                return self._guarded(lambda p: {"results": p.search(text)})
            if path.startswith("/apps/"):
                name = path[len("/apps/"):].removesuffix(".html").strip("/")
                file = holder.project.root / "apps" / f"{name}.html"
                if _APP_NAME.match(name) and file.is_file():
                    return self._send(200, file.read_bytes(), "text/html; charset=utf-8")
            self._json({"error": f"not found: {path}"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorised():
                return
            if urlparse(self.path).path != "/api/query":
                return self._json({"error": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
            except ValueError as e:
                return self._json({"error": f"bad request: {e}", "kind": "request"}, 400)
            self._guarded(lambda p: run_query(p, body, allow_adhoc))

    return Handler


def make_server(project_path: str | Path, host: str = "127.0.0.1", port: int = 7979,
                allow_adhoc: bool = False, user: str = "otab",
                password: str | None = None) -> ThreadingHTTPServer:
    holder = ProjectHolder(project_path)
    holder.project.connect()
    server = ThreadingHTTPServer((host, port), make_handler(holder, allow_adhoc, user, password))
    server.holder = holder  # type: ignore[attr-defined]
    return server
