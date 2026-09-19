"""Private data-directory routing for local and hosted users.

The mature local schema contains global provider identifiers and URLs. Hosted
users therefore receive separate SQLite/config/media roots rather than relying
on every future SQL query to remember a tenant predicate.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from . import paths
from .auth import Identity


HOSTED_ROOT_ENV = "LOCAL_CALENDAR_HOSTED_ROOT"
OWNER_EMAIL_ENV = "LOCAL_CALENDAR_OWNER_EMAIL"
_TENANT_ID = re.compile(r"^[0-9a-f]{32}$")


class TenancyConfigurationError(Exception):
    """Hosted tenant storage is absent or unsafe to use."""


@dataclass(frozen=True)
class TenantPaths:
    id: str
    email: str
    role: str
    root: Path
    is_local: bool = False

    @property
    def db_path(self) -> Path:
        return self.root / "calendar.db"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def avatar_dir(self) -> Path:
        return self.root / "avatars"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def env_local_path(self) -> Path:
        return self.root / ".env.local"

    def ensure(self) -> "TenantPaths":
        for directory in (self.root, self.media_dir, self.avatar_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        return self


def local(email: str = "local@localhost") -> TenantPaths:
    return TenantPaths(
        id="local", email=email, role="owner", root=paths.HOME, is_local=True,
    )


_current: contextvars.ContextVar[TenantPaths | None] = contextvars.ContextVar(
    "local_calendar_tenant", default=None
)


def current() -> TenantPaths:
    return _current.get() or local()


def set_current(tenant: TenantPaths):
    return _current.set(tenant)


def reset_current(token) -> None:
    _current.reset(token)


@contextmanager
def activate(tenant: TenantPaths) -> Iterator[TenantPaths]:
    token = set_current(tenant)
    try:
        yield tenant
    finally:
        reset_current(token)


def _hosted_root(env: Mapping[str, str]) -> Path:
    raw = (env.get(HOSTED_ROOT_ENV) or "").strip()
    if not raw:
        raise TenancyConfigurationError(f"{HOSTED_ROOT_ENV} is required")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise TenancyConfigurationError(f"{HOSTED_ROOT_ENV} must be an absolute path")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _owner_email(env: Mapping[str, str]) -> str:
    value = (env.get(OWNER_EMAIL_ENV) or "").strip().lower()
    if not value or "@" not in value:
        raise TenancyConfigurationError(f"{OWNER_EMAIL_ENV} is required")
    return value


def _registry(root: Path) -> sqlite3.Connection:
    path = root / "registry.db"
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_user (
            id             TEXT PRIMARY KEY,
            access_subject TEXT NOT NULL UNIQUE,
            email          TEXT NOT NULL UNIQUE COLLATE NOCASE,
            role           TEXT NOT NULL CHECK(role IN ('owner','member')),
            created_at     TEXT NOT NULL
        );
    """)
    conn.commit()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


def resolve(identity: Identity,
            environ: Mapping[str, str] | None = None) -> TenantPaths:
    """Resolve or provision the private root for a verified identity."""
    if identity.mode == "local":
        return local(identity.email)
    if identity.mode != "cloudflare":
        raise TenancyConfigurationError("tenant storage requires a known auth mode")

    env = environ if environ is not None else os.environ
    root = _hosted_root(env)
    owner_email = _owner_email(env)
    role = "owner" if identity.email == owner_email else "member"
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    conn = _registry(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM app_user WHERE access_subject=?", (identity.subject,)
        ).fetchone()
        if row is None:
            tenant_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO app_user "
                "(id,access_subject,email,role,created_at) VALUES (?,?,?,?,?)",
                (tenant_id, identity.subject, identity.email, role, now),
            )
            row = conn.execute(
                "SELECT * FROM app_user WHERE access_subject=?", (identity.subject,)
            ).fetchone()
        elif row["email"].lower() != identity.email:
            # Access subjects are stable across an email change. Preserve the
            # tenant and update the address used for administration/reporting.
            conn.execute(
                "UPDATE app_user SET email=?, role=? WHERE id=?",
                (identity.email, role, row["id"]),
            )
            row = conn.execute(
                "SELECT * FROM app_user WHERE id=?", (row["id"],)
            ).fetchone()
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise TenancyConfigurationError(
            "the authenticated email is already attached to another Access identity"
        ) from exc
    finally:
        conn.close()

    tenant_id = str(row["id"])
    if not _TENANT_ID.fullmatch(tenant_id):
        raise TenancyConfigurationError("registry contains an invalid tenant id")
    return TenantPaths(
        id=tenant_id,
        email=str(row["email"]),
        role=str(row["role"]),
        root=root / "tenants" / tenant_id,
    ).ensure()
