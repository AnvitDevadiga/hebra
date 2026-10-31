"""Incremental repository indexing.

Each run takes the index write lock (``BEGIN IMMEDIATE``) *before* reading
the current index state, so concurrent runs are serialised: a second run
waits for the first to commit (up to the busy timeout) and then sees its
result. Within that single transaction:

* unchanged files (same SHA-256) are left untouched,
* changed files have their rows replaced,
* new files are inserted,
* files no longer present (or now excluded) are deleted.

A failure, including Ctrl-C, rolls the transaction back, leaving the
previous index intact. If the index format changed (see
``store.INDEX_FORMAT_VERSION``), all rows are discarded and rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from hebra import __version__
from hebra.config import IndexConfig
from hebra.indexing import store
from hebra.indexing.chunking import Chunk, split_lines, text_chunks
from hebra.indexing.gitmeta import read_head_revision
from hebra.indexing.python_parser import Symbol, parse_python
from hebra.indexing.scanner import ScanStats, SourceFile, scan_repository
from hebra.paths import IndexLocation, index_location, resolve_repository
from hebra.terms import camel_parts, identifier_subwords

_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".md": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".ini": "ini",
    ".cfg": "ini",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".xml": "xml",
}


@dataclass
class IndexReport:
    repo_root: str
    index_path: str
    head_revision: str | None
    file_source: str
    files_indexed: int = 0
    files_added: int = 0
    files_changed: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    chunks_written: int = 0
    python_parse_failures: int = 0
    rebuilt: bool = False
    candidates: int = 0
    bytes_read: int = 0
    truncated: bool = False
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def detect_language(path: str) -> str:
    return _LANGUAGES.get(PurePosixPath(path).suffix.lower(), "text")


def _chunk_symbol_names(chunk: Chunk, symbols: list[Symbol]) -> str:
    names: set[str] = set()
    if chunk.symbol:
        names.add(chunk.symbol)
    for sym in symbols:
        if chunk.start_line <= sym.start_line <= chunk.end_line:
            names.add(sym.qualname)
    expanded = set(names)
    for name in names:
        for piece in name.replace("<locals>", " ").replace(".", " ").replace("_", " ").split():
            expanded.update(camel_parts(piece))
    return " ".join(sorted(expanded))


def _insert_file(
    conn: sqlite3.Connection, source: SourceFile, config: IndexConfig
) -> tuple[int, bool]:
    """Insert one file and its derived rows. Returns (chunk count, parse failed)."""
    language = detect_language(source.path)
    symbols: list[Symbol] = []
    imports = []
    parse_failed = False
    if language == "python":
        parsed = parse_python(source.text, config.chunk)
        if parsed is None:
            parser = "text-fallback"
            parse_failed = True
            chunks = text_chunks(source.text, config.chunk)
        else:
            parser = "python-ast"
            symbols, imports, chunks = parsed.symbols, parsed.imports, parsed.chunks
    else:
        parser = "text"
        chunks = text_chunks(source.text, config.chunk)

    cursor = conn.execute(
        "INSERT INTO files(path, sha256, size_bytes, line_count, language, parser) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            source.path,
            source.sha256,
            source.size_bytes,
            len(split_lines(source.text)),
            language,
            parser,
        ),
    )
    file_id = cursor.lastrowid
    conn.executemany(
        "INSERT INTO symbols(file_id, name, qualname, kind, start_line, end_line, docstring) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (file_id, s.name, s.qualname, s.kind, s.start_line, s.end_line, s.docstring)
            for s in symbols
        ],
    )
    conn.executemany(
        "INSERT INTO imports(file_id, module, name, alias, line) VALUES (?, ?, ?, ?, ?)",
        [(file_id, i.module, i.name, i.alias, i.line) for i in imports],
    )

    path_terms = " ".join([source.path, *camel_parts(PurePosixPath(source.path).stem)])
    for chunk in chunks:
        cur = conn.execute(
            "INSERT INTO chunks(file_id, start_line, end_line, kind, symbol, content, "
            "content_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                chunk.start_line,
                chunk.end_line,
                chunk.kind,
                chunk.symbol,
                chunk.content,
                hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
            ),
        )
        conn.execute(
            "INSERT INTO chunks_fts(rowid, path, symbols, identifiers, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                cur.lastrowid,
                path_terms,
                _chunk_symbol_names(chunk, symbols),
                identifier_subwords(chunk.content),
                chunk.content,
            ),
        )
    return len(chunks), parse_failed


def index_repository(
    repo_path: Path,
    db_path: Path | None = None,
    config: IndexConfig | None = None,
) -> IndexReport:
    """Build or incrementally update the index for ``repo_path``.

    ``db_path`` is an explicit index location; by default the index lives in
    ``<repo>/.hebra/``. See :mod:`hebra.paths` for the safety policy.
    """
    config = config or IndexConfig()
    root = resolve_repository(repo_path)
    location = index_location(root, db_path)

    conn = store.open_for_write(location)
    stats = ScanStats()
    report = IndexReport(
        repo_root=str(root),
        index_path=str(location.path),
        head_revision=read_head_revision(root),
        file_source="",
    )
    try:
        with store.database_errors(location.path):
            conn.execute("BEGIN IMMEDIATE")
            try:
                _apply_scan(conn, root, location, config, stats, report)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
    return report


def _apply_scan(
    conn: sqlite3.Connection,
    root: Path,
    location: IndexLocation,
    config: IndexConfig,
    stats: ScanStats,
    report: IndexReport,
) -> None:
    report.rebuilt = store.ensure_schema(conn, location, config.chunk)
    existing = {
        row["path"]: (row["id"], row["sha256"])
        for row in conn.execute("SELECT id, path, sha256 FROM files")
    }
    seen: set[str] = set()
    for source in scan_repository(root, config.scan, stats):
        seen.add(source.path)
        previous = existing.get(source.path)
        if previous is not None and previous[1] == source.sha256:
            report.files_unchanged += 1
            continue
        if previous is not None:
            store.delete_file(conn, previous[0])
            report.files_changed += 1
        else:
            report.files_added += 1
        written, failed = _insert_file(conn, source, config)
        report.chunks_written += written
        report.python_parse_failures += int(failed)

    for path in sorted(existing.keys() - seen):
        store.delete_file(conn, existing[path][0])
        report.files_removed += 1

    report.files_indexed = len(seen)
    report.file_source = stats.file_source
    report.candidates = stats.candidates
    report.bytes_read = stats.bytes_read
    report.truncated = stats.truncated
    report.skipped = dict(sorted(stats.skipped.items()))
    report.warnings = list(stats.warnings)
    if report.rebuilt:
        report.warnings.append("index format changed since the last run; rebuilt from scratch")
    if report.python_parse_failures:
        report.warnings.append(
            f"{report.python_parse_failures} Python file(s) failed to parse and were "
            "indexed as plain text without symbols"
        )

    store.set_meta(
        conn,
        {
            "hebra_version": __version__,
            "repo_root": str(root),
            "head_revision": report.head_revision or "",
            "indexed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "last_run": json.dumps(report.to_dict(), sort_keys=True),
        },
    )


def index_summary(repo_path: Path, db_path: Path | None = None) -> dict[str, Any]:
    """Summarise an existing index without reading repository files."""
    location = index_location(resolve_repository(repo_path), db_path)
    db_path = location.path
    conn = store.open_for_read(location)
    try:
        with store.database_errors(db_path):
            meta = store.get_meta(conn)

            def counts(sql: str) -> dict[str, int]:
                return {row[0]: row[1] for row in conn.execute(sql)}

            totals = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0), COALESCE(SUM(line_count), 0) "
                "FROM files"
            ).fetchone()
            last_run = json.loads(meta["last_run"]) if "last_run" in meta else {}
            return {
                "index_path": str(db_path),
                "index_size_bytes": db_path.stat().st_size,
                "schema_version": store.SCHEMA_VERSION,
                "index_format_version": store.INDEX_FORMAT_VERSION,
                "hebra_version": meta.get("hebra_version"),
                "repo_root": meta.get("repo_root"),
                "head_revision": meta.get("head_revision") or None,
                "indexed_at": meta.get("indexed_at"),
                "files": totals[0],
                "source_bytes": totals[1],
                "source_lines": totals[2],
                "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                "symbols": conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
                "imports": conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0],
                "files_by_language": counts(
                    "SELECT language, COUNT(*) FROM files GROUP BY language ORDER BY language"
                ),
                "files_by_parser": counts(
                    "SELECT parser, COUNT(*) FROM files GROUP BY parser ORDER BY parser"
                ),
                "chunks_by_kind": counts(
                    "SELECT kind, COUNT(*) FROM chunks GROUP BY kind ORDER BY kind"
                ),
                "symbols_by_kind": counts(
                    "SELECT kind, COUNT(*) FROM symbols GROUP BY kind ORDER BY kind"
                ),
                "last_run": {
                    key: last_run.get(key)
                    for key in ("file_source", "candidates", "skipped", "truncated", "warnings")
                },
            }
    finally:
        conn.close()
