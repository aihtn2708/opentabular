"""Row-level security and sign-in.

Roles come from the model (TMDL `role` objects with `tablePermission`
filters). Which person holds which role is deployment configuration, kept in
`opentabular.yml`:

    security:
      users:
        - name: alice
          password_hash: pbkdf2_sha256$600000$...   # from `otab hash-password`
          roles: [Vietnam Sales]
        - name: owner
          password_hash: pbkdf2_sha256$600000$...
          admin: true                                # sees everything

How roles combine (as in Power BI): for each table, a person sees the rows
allowed by ANY of their roles; a role with no filter on a table allows all of
its rows. Filters on different tables all apply. The filters are added to
every query below the DAX, so ALL() and CALCULATE cannot remove them.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass, field

from .model import Model

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 600_000


class SecurityError(Exception):
    pass


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{_ALGO}${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != _ALGO:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


@dataclass
class Identity:
    """Who is asking. `admin` bypasses row-level security."""

    name: str | None = None
    roles: list[str] = field(default_factory=list)
    admin: bool = False


@dataclass
class User:
    name: str
    password_hash: str
    roles: list[str] = field(default_factory=list)
    admin: bool = False


def load_users(config: dict, model: Model) -> list[User]:
    users: list[User] = []
    for spec in ((config.get("security") or {}).get("users") or []):
        if not isinstance(spec, dict) or not spec.get("name") or not spec.get("password_hash"):
            raise SecurityError("each security.users entry needs a name and a password_hash")
        roles = [str(r) for r in (spec.get("roles") or [])]
        for r in roles:
            if r.casefold() not in model.roles:
                raise SecurityError(f"user {spec['name']!r}: the model has no role named {r!r}")
        users.append(User(str(spec["name"]), str(spec["password_hash"]), roles, bool(spec.get("admin"))))
    if len({u.name.casefold() for u in users}) != len(users):
        raise SecurityError("security.users lists the same name twice")
    return users


def row_filters(model: Model, identity: Identity | None) -> dict[str, str]:
    """The DAX condition per table that this identity's queries must respect."""
    if identity is None or identity.admin:
        return {}
    roles = []
    for name in identity.roles:
        role = model.roles.get(name.casefold())
        if role is None:
            raise SecurityError(f"the model has no role named {name!r}")
        roles.append(role)
    if not model.roles:
        return {}
    if not roles:
        raise SecurityError(
            "this model has security roles and "
            + (f"user {identity.name!r}" if identity.name else "the caller")
            + " holds none of them"
        )
    out: dict[str, str] = {}
    tables = {t.casefold(): t for role in roles for t in role.table_filters}
    for key, table in tables.items():
        parts = []
        for role in roles:
            dax = next((f for t, f in role.table_filters.items() if t.casefold() == key), None)
            if dax is None:  # this role allows every row of the table
                parts = []
                break
            parts.append(f"({dax})")
        if parts:
            out[table] = " || ".join(parts)
    return out
