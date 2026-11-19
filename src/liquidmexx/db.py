"""SQLite connection, additive migrations and consistent backups."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from liquidmexx.settings import PROJECT_ROOT, get_settings
from liquidmexx.timeutil import now_iso

log = logging.getLogger(__name__)

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
BACKUP_KEEP = 7


class MigrationError(RuntimeError):
    pass


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else get_settings().db_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)  # autocommit; explicit txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _migration_files(directory: Path) -> list[tuple[str, Path]]:
    files = sorted(p for p in directory.glob("*.sql") if p.name[:4].isdigit())
    return [(p.stem, p) for p in files]


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrate(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
    backup_dir: Path | None = None,
) -> list[str]:
    """Apply pending migrations in order. Never re-applies or edits applied ones.

    Refuses to run if an already-applied migration file was modified, so the
    schema history stays reproducible. Takes a backup first when the database
    already holds data and something is pending.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL)"
    )
    applied = {
        row["version"]: row["checksum"]
        for row in conn.execute("SELECT version, checksum FROM schema_migrations")
    }
    files = _migration_files(migrations_dir)
    for version, path in files:
        if version in applied and applied[version] != _checksum(path):
            raise MigrationError(
                f"migration {version} was modified after being applied; "
                "add a new migration instead of editing an old one"
            )
    pending = [(v, p) for v, p in files if v not in applied]
    if not pending:
        return []

    if applied and backup_dir is not None:
        backup(conn, backup_dir, label="pre-migrate")

    done: list[str] = []
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        try:
            conn.execute("BEGIN")
            for statement in _split_sql(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) VALUES (?,?,?)",
                (version, now_iso(), _checksum(path)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        log.info("applied migration %s", version)
        done.append(version)
    return done


def _split_sql(script: str) -> list[str]:
    """Split a migration script into complete statements (trigger-aware)."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise MigrationError("migration ends with an incomplete statement")
    return statements


def backup(conn: sqlite3.Connection, backup_dir: Path, label: str = "auto") -> Path:
    """Write a consistent online snapshot using SQLite's backup API; keep the newest N."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"liquidmexx-{stamp}-{label}.db"
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    snapshots = sorted(backup_dir.glob("liquidmexx-*.db"))
    for old in snapshots[:-BACKUP_KEEP]:
        old.unlink()  # only our own timestamped snapshot files
    log.info("backup written: %s", target.name)
    return target


def open_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Connect and bring the schema up to date (with a pre-migration backup)."""
    conn = connect(db_path)
    backup_dir = get_settings().data_dir / "backups" if db_path is None else None
    migrate(conn, backup_dir=backup_dir)
    return conn
