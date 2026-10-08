from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..core.schema import OperatorError

INTENTS = frozenset({"active", "paused", "maintenance", "stopped"})
SCHEMA_VERSION = 1


class AuthorityDenied(OperatorError):
    """No durable operator authorization permits this effect."""


def project_identity(repo: Path) -> str:
    return hashlib.sha256(os.fsencode(repo.expanduser().resolve())).hexdigest()


class AuthorityRegistry:
    """Operator-owned journal. Opening it for admission never creates authority.

    The caller must bind repo from its trusted connection, never a request payload.
    Filesystem confinement must protect this journal from untrusted children;
    this registry alone does not provide an OS security boundary.
    """

    def __init__(self, path: Path):
        self.path = path.expanduser().absolute()

    def _check_path(self) -> None:
        for path in (self.path, *self.path.parents):
            if path.is_symlink():
                raise AuthorityDenied("operator authority path contains a symlink")
        if self.path.exists() and not self.path.is_file():
            raise AuthorityDenied("operator authority is not a regular file")

    def initialize(self) -> None:
        """Explicit trusted operator action; contains no default project grants."""
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        with closing(sqlite3.connect(self.path)) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise AuthorityDenied("unsupported operator authority schema")
            db.execute("CREATE TABLE IF NOT EXISTS projects (identity TEXT PRIMARY KEY, "
                       "repo TEXT NOT NULL UNIQUE, intent TEXT NOT NULL CHECK(intent IN "
                       "('active','paused','maintenance','stopped')), epoch INTEGER NOT NULL, "
                       "goal_revision TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _connection(self, *, writable: bool = False) -> Iterator[sqlite3.Connection]:
        self._check_path()
        if not self.path.is_file():
            raise AuthorityDenied("operator authority is missing")
        try:
            uri = self.path.as_uri() + ("?mode=rw" if writable else "?mode=ro")
            with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db, db:
                if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise AuthorityDenied("unsupported operator authority schema")
                yield db
        except sqlite3.Error as exc:
            raise AuthorityDenied("operator authority is unavailable or corrupt") from exc

    def set_intent(self, repo: Path, intent: str, *, goal_revision: str) -> int:
        """Trusted operator mutation. Never expose this method through script IPC."""
        if intent not in INTENTS or not goal_revision.strip():
            raise ValueError("valid operator intent and accepted goal revision are required")
        canonical = str(repo.expanduser().resolve())
        identity = project_identity(repo)
        with self._connection(writable=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT epoch FROM projects WHERE identity=?", (identity,)).fetchone()
            epoch = row[0] + 1 if row else 1
            db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?) ON CONFLICT(identity) "
                       "DO UPDATE SET intent=excluded.intent, epoch=excluded.epoch, "
                       "goal_revision=excluded.goal_revision, updated_at=excluded.updated_at",
                       (identity, canonical, intent, epoch, goal_revision,
                        datetime.now(timezone.utc).isoformat()))
            return epoch

    def status(self, repo: Path) -> dict[str, object]:
        with self._connection() as db:
            row = db.execute("SELECT repo,intent,epoch,goal_revision,updated_at FROM projects "
                             "WHERE identity=?", (project_identity(repo),)).fetchone()
        if row is None or row[0] != str(repo.expanduser().resolve()) or row[1] not in INTENTS:
            raise AuthorityDenied("project has no valid operator authorization")
        return dict(zip(("repo", "intent", "epoch", "goal_revision", "updated_at"), row))

    @contextmanager
    def admit(self, repo: Path, action: str) -> Iterator[dict[str, object]]:
        """Serialize one effect boundary against operator pause.

        Hold only through the admission/effect initiation, never for an entire
        subprocess lifetime. An admitted execution may drain after pause.
        """
        if not action.strip():
            raise ValueError("effect action is required")
        with self._connection(writable=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT repo,intent,epoch,goal_revision FROM projects WHERE identity=?",
                             (project_identity(repo),)).fetchone()
            if row is None or row[0] != str(repo.expanduser().resolve()) or row[1] != "active":
                raise AuthorityDenied(f"operator authority denies {action}")
            yield dict(zip(("repo", "intent", "epoch", "goal_revision"), row))
