"""SQLite storage for the repository index.

All SQL text is static; every value is passed as a bound parameter.
Connections run in autocommit mode and callers manage transactions
explicitly (``BEGIN IMMEDIATE`` for writers), so the write lock is held
before any index state is read.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hebra.config import ChunkConfig
from hebra.errors import IndexDatabaseError, IndexNotFoundError, IndexPathError, IndexVersionError
from hebra.paths import IndexLocation, prepare_index

# Table layout. A mismatch on read is an error; on index it forces a rebuild.
SCHEMA_VERSION = 2
# Version of everything that derives stored rows from source: scanning rules
# that change stored content, parsing, chunking, and FTS column derivation.
# Bump on any such change so old chunks are never silently reused.
INDEX_FORMAT_VERSION = 3
# PRAGMA application_id marking a database as a Hebra index ("HEBR").
APPLICATION_ID = 0x48454252
WRITE_BUSY_TIMEOUT_SECONDS = 30.0
READ_BUSY_TIMEOUT_SECONDS = 5.0

_SCHEMA_STATEMENTS = (
    """CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE files (
        id          INTEGER PRIMARY KEY,
        path        TEXT NOT NULL UNIQUE,
        sha256      TEXT NOT NULL,
        size_bytes  INTEGER NOT NULL,
        line_count  INTEGER NOT NULL,
        language    TEXT NOT NULL,
        parser      TEXT NOT NULL,
        module      TEXT,
        is_test     INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE symbols (
        id          INTEGER PRIMARY KEY,
        file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        symbol_id   TEXT NOT NULL UNIQUE,
        name        TEXT NOT NULL,
        qualname    TEXT NOT NULL,
        kind        TEXT NOT NULL,
        start_line  INTEGER NOT NULL,
        end_line    INTEGER NOT NULL,
        docstring   TEXT
    )""",
    "CREATE INDEX symbols_file ON symbols(file_id)",
    "CREATE INDEX symbols_name ON symbols(name)",
    """CREATE TABLE imports (
        id       INTEGER PRIMARY KEY,
        file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        module   TEXT NOT NULL,
        name     TEXT,
        alias    TEXT,
        line     INTEGER NOT NULL
    )""",
    "CREATE INDEX imports_file ON imports(file_id)",
    """CREATE TABLE refs (
        id       INTEGER PRIMARY KEY,
        file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        scope    TEXT NOT NULL,
        kind     TEXT NOT NULL,
        expr     TEXT NOT NULL,
        line     INTEGER NOT NULL
    )""",
    "CREATE INDEX refs_file ON refs(file_id)",
    """CREATE TABLE edges (
        id      INTEGER PRIMARY KEY,
        src     TEXT NOT NULL,
        dst     TEXT,
        kind    TEXT NOT NULL,
        status  TEXT NOT NULL,
        target  TEXT NOT NULL,
        line    INTEGER NOT NULL,
        detail  TEXT NOT NULL
    )""",
    "CREATE INDEX edges_src ON edges(src, status, kind)",
    "CREATE INDEX edges_dst ON edges(dst, status, kind)",
    """CREATE TABLE chunks (
        id              INTEGER PRIMARY KEY,
        file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        start_line      INTEGER NOT NULL,
        end_line        INTEGER NOT NULL,
        kind            TEXT NOT NULL,
        symbol          TEXT,
        content         TEXT NOT NULL,
        content_sha256  TEXT NOT NULL
    )""",
    "CREATE INDEX chunks_file ON chunks(file_id)",
    """CREATE VIRTUAL TABLE chunks_fts USING fts5(
        path, symbols, identifiers, body,
        tokenize = 'unicode61 remove_diacritics 2'
    )""",
)


@contextmanager
def database_errors(path: Path) -> Iterator[None]:
    """Translate operational SQLite failures into :class:`IndexDatabaseError`.

    ``IntegrityError`` and ``ProgrammingError`` indicate bugs in Hebra, not
    environmental problems, so they are deliberately left untouched.
    """
    try:
        yield
    except (sqlite3.IntegrityError, sqlite3.ProgrammingError):
        raise
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if "locked" in message or "busy" in message:
            raise IndexDatabaseError(
                f"index {path} is locked by another Hebra process; try again when it finishes"
            ) from exc
        if "fts5" in message.lower():
            raise IndexDatabaseError(
                "this Python's SQLite library was built without FTS5, which Hebra requires"
            ) from exc
        raise IndexDatabaseError(f"index database error at {path}: {message}") from exc
    except sqlite3.DatabaseError as exc:
        raise IndexDatabaseError(
            f"index at {path} is not a usable SQLite database ({exc}); delete it and re-run "
            "`hebra index`"
        ) from exc


def _connect(location: IndexLocation, mode: str, timeout: float) -> sqlite3.Connection:
    conn = sqlite3.connect(
        location.path.as_uri() + f"?mode={mode}",
        uri=True,
        timeout=timeout,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _pragma_int(conn: sqlite3.Connection, name: str) -> int:
    # ``name`` is always one of a few internal constants.
    return int(conn.execute(f"PRAGMA {name}").fetchone()[0])


def _classify(conn: sqlite3.Connection) -> str:
    """Return "hebra", "empty", or "foreign" for the opened database."""
    if _pragma_int(conn, "application_id") == APPLICATION_ID:
        return "hebra"
    if conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0:
        return "empty"
    # Indexes written before APPLICATION_ID existed (Hebra 0.1.0 pre-release).
    if _pragma_int(conn, "application_id") == 0 and _pragma_int(conn, "user_version") == 1:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if has_meta and conn.execute("SELECT 1 FROM meta WHERE key = 'hebra_version'").fetchone():
            return "hebra"
    return "foreign"


def _foreign(path: Path) -> IndexPathError:
    return IndexPathError(
        f"{path} is an existing SQLite database that is not a Hebra index; refusing to modify "
        "it. Choose a different --db path."
    )


def chunk_config_fingerprint(config: ChunkConfig) -> str:
    return json.dumps(
        {
            "max_lines": config.max_lines,
            "max_chars": config.max_chars,
            "text_window_lines": config.text_window_lines,
        },
        sort_keys=True,
    )


def open_for_write(location: IndexLocation) -> sqlite3.Connection:
    """Safely open (creating if needed) the index database for writing.

    The schema is not touched here; call :func:`ensure_schema` inside the
    caller's write transaction.
    """
    prepared = prepare_index(location, create=True)
    with database_errors(location.path):
        conn = _connect(location, "rw", WRITE_BUSY_TIMEOUT_SECONDS)
    try:
        prepared.verify_unchanged()
    except BaseException:
        conn.close()
        raise
    return conn


def _drop_all(conn: sqlite3.Connection) -> None:
    def quoted(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    virtual = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL%'"
        )
    ]
    for name in virtual:  # also drops their shadow tables
        conn.execute(f"DROP TABLE {quoted(name)}")
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for name in tables:
        conn.execute(f"DROP TABLE {quoted(name)}")


def ensure_schema(
    conn: sqlite3.Connection, location: IndexLocation, chunk_config: ChunkConfig
) -> bool:
    """Create the schema, or rebuild it if the index format changed.

    Must run inside a write transaction. Returns True if existing data was
    discarded because it was written in an incompatible format.
    """
    kind = _classify(conn)
    if kind == "foreign":
        raise _foreign(location.path)
    chunk_fp = chunk_config_fingerprint(chunk_config)
    rebuilt = False
    if kind == "hebra":
        meta = get_meta(conn)
        current = (
            _pragma_int(conn, "user_version") == SCHEMA_VERSION
            and meta.get("index_format_version") == str(INDEX_FORMAT_VERSION)
            and meta.get("chunk_config") == chunk_fp
        )
        if current:
            return False
        _drop_all(conn)
        rebuilt = True
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID:d}")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
    set_meta(conn, {"index_format_version": str(INDEX_FORMAT_VERSION), "chunk_config": chunk_fp})
    return rebuilt


def open_for_read(location: IndexLocation) -> sqlite3.Connection:
    """Open an existing index read-only after validating its path and format."""
    prepared = prepare_index(location, create=False)
    if prepared.file_identity is None:
        raise IndexNotFoundError(
            f"no index found at {location.path}; run `hebra index <repository-path>` first"
        )
    with database_errors(location.path):
        conn = _connect(location, "ro", READ_BUSY_TIMEOUT_SECONDS)
        try:
            prepared.verify_unchanged()
            kind = _classify(conn)
            if kind == "foreign":
                raise _foreign(location.path)
            if kind == "empty":
                raise IndexNotFoundError(
                    f"index at {location.path} is empty; run `hebra index <repository-path>`"
                )
            meta = get_meta(conn)
            version = _pragma_int(conn, "user_version")
            fmt = meta.get("index_format_version")
            if version != SCHEMA_VERSION or fmt != str(INDEX_FORMAT_VERSION):
                raise IndexVersionError(
                    f"index at {location.path} uses an older or incompatible format "
                    f"(schema {version}, format {fmt or 'unknown'}; expected schema "
                    f"{SCHEMA_VERSION}, format {INDEX_FORMAT_VERSION}); re-run `hebra index` "
                    "to rebuild it"
                )
        except BaseException:
            conn.close()
            raise
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
