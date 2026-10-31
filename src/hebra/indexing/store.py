"""SQLite storage for the repository index.

All SQL text is static; every value is passed as a bound parameter.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hebra.errors import HebraError, IndexNotFoundError, IndexVersionError

# Bump whenever the schema or the meaning of stored data (chunking, parsing,
# FTS columns) changes. A mismatch forces a full rebuild on the next index.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE files (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    line_count  INTEGER NOT NULL,
    language    TEXT NOT NULL,
    parser      TEXT NOT NULL
);
CREATE TABLE symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    qualname    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    docstring   TEXT
);
CREATE INDEX symbols_file ON symbols(file_id);
CREATE INDEX symbols_name ON symbols(name);
CREATE TABLE imports (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    module   TEXT NOT NULL,
    name     TEXT,
    alias    TEXT,
    line     INTEGER NOT NULL
);
CREATE INDEX imports_file ON imports(file_id);
CREATE TABLE chunks (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    symbol          TEXT,
    content         TEXT NOT NULL,
    content_sha256  TEXT NOT NULL
);
CREATE INDEX chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    path, symbols, identifiers, body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def _connect(uri: str) -> sqlite3.Connection:
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def open_for_write(db_path: Path) -> sqlite3.Connection:
    """Open (creating or rebuilding as needed) an index database for writing."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path.resolve().as_uri() + "?mode=rwc")
    version = _schema_version(conn)
    if version == SCHEMA_VERSION:
        return conn
    if version != 0:
        # Incompatible older/newer index: rebuild from scratch.
        conn.close()
        db_path.unlink()
        conn = _connect(db_path.resolve().as_uri() + "?mode=rwc")
    try:
        with conn:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
    except sqlite3.OperationalError as exc:
        conn.close()
        if "fts5" in str(exc).lower():
            raise HebraError(
                "this Python's SQLite library was built without FTS5, which Hebra requires"
            ) from exc
        raise
    return conn


def open_for_read(db_path: Path) -> sqlite3.Connection:
    """Open an existing index read-only, validating its schema version."""
    if not db_path.is_file():
        raise IndexNotFoundError(
            f"no index found at {db_path}; run `hebra index <repository-path>` first"
        )
    conn = _connect(db_path.resolve().as_uri() + "?mode=ro")
    try:
        version = _schema_version(conn)
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise IndexVersionError(f"{db_path} is not a readable Hebra index: {exc}") from exc
    if version != SCHEMA_VERSION:
        conn.close()
        raise IndexVersionError(
            f"index at {db_path} has schema version {version}, expected {SCHEMA_VERSION}; "
            "re-run `hebra index` to rebuild it"
        )
    return conn


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}


def set_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        sorted(values.items()),
    )


def delete_file(conn: sqlite3.Connection, file_id: int) -> None:
    conn.execute(
        "DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE file_id = ?)",
        (file_id,),
    )
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
