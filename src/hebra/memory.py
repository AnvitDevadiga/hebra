"""Source-grounded repository memory.

A small store of facts about a repository that a person (or a tool acting
for them) wants carried into future context: architectural facts,
conventions, failure causes, relationships, decisions, test observations.
It is not a conversation log and nothing in it is learned automatically.

Provenance (fixed when a record is added):

``source-verified``  A non-decision record with source references. Each
                     reference was read from the working tree at creation and
                     its file and span SHA-256 were stored.
``user-decision``    A ``decision`` record: an explicit engineering decision.
                     It is shown as a decision, never as verified source fact.
``unverified``       A non-decision record without source references. It can
                     be listed but is never included in packed context.

Freshness (recomputed on every read; nothing is trusted from the past):

``current``        every reference still matches its stored span hash at the
                   recorded lines
``stale``          some reference changed, moved, was renamed, deleted, or
                   replaced by a non-regular file
``decision``       a user decision (reference drift is reported as notes)
``unverifiable``   no references to check
``invalidated``    explicitly invalidated by the user

Revision: each record stores HEAD at creation. If that commit is no longer
in HEAD's history (branch switch, reset), this is reported; status remains
content-based, since an unchanged span is still accurate source evidence.

Repository identity: the store records the repository's root commit(s), or
its resolved path when there is no Git history. Opening a store from a
different repository raises :class:`~hebra.errors.MemoryIdentityError`.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from hebra.errors import HebraError, MemoryIdentityError, MemoryStoreError
from hebra.indexing.gitmeta import is_ancestor, read_head_revision, root_commits
from hebra.indexing.scanner import read_repository_file
from hebra.paths import IndexLocation, memory_location, prepare_index, resolve_repository
from hebra.terms import word_set

KINDS = ("fact", "convention", "failure", "relationship", "decision", "test-observation")
APPLICATION_ID = 0x4842524D  # "HBRM"
SCHEMA_VERSION = 1
MAX_CONTENT_CHARS = 4_000
MAX_REFS = 20
MAX_SPAN_LINES = 2_000
MAX_REF_FILE_BYTES = 10_000_000
_REF = re.compile(r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")

_SCHEMA = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE records (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        provenance TEXT NOT NULL,
        git_revision TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL,
        invalidated_reason TEXT
    )""",
    """CREATE TABLE refs (
        record_id TEXT NOT NULL REFERENCES records(id),
        ordinal INTEGER NOT NULL,
        path TEXT NOT NULL,
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        file_sha256 TEXT NOT NULL,
        span_sha256 TEXT NOT NULL,
        PRIMARY KEY (record_id, ordinal)
    )""",
)


@dataclass(frozen=True)
class SourceRef:
    path: str
    start_line: int
    end_line: int
    file_sha256: str
    span_sha256: str


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    kind: str
    content: str
    provenance: str
    git_revision: str | None
    created_at: str
    updated_at: str
    status: str  # "active" or "invalidated"
    invalidated_reason: str | None
    refs: tuple[SourceRef, ...]


@dataclass(frozen=True)
class RefCheck:
    ref: SourceRef
    status: str  # current, moved, changed, renamed, missing, replaced
    detail: str = ""


@dataclass
class MemoryView:
    """A record plus its freshness, computed now."""

    record: MemoryRecord
    status: str
    checks: list[RefCheck] = field(default_factory=list)
    revision_note: str = ""

    @property
    def terms(self) -> frozenset[str]:
        return frozenset(word_set(self.record.content))

    def to_dict(self) -> dict[str, object]:
        r = self.record
        return {
            "id": r.id,
            "kind": r.kind,
            "content": r.content,
            "provenance": r.provenance,
            "status": self.status,
            "git_revision": r.git_revision,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "invalidated_reason": r.invalidated_reason,
            "revision_note": self.revision_note,
            "refs": [
                {
                    "path": c.ref.path,
                    "start_line": c.ref.start_line,
                    "end_line": c.ref.end_line,
                    "file_sha256": c.ref.file_sha256,
                    "span_sha256": c.ref.span_sha256,
                    "status": c.status,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
        }


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lines(data: bytes) -> list[bytes]:
    text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = text.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def _span_hash(lines: list[bytes], start: int, end: int) -> str:
    return _sha(b"\n".join(lines[start - 1 : end]))


def repository_identity(root: Path) -> dict[str, str]:
    roots = root_commits(root)
    if roots:
        return {"root_commits": ",".join(roots)}
    return {"root_path": str(root)}


class MemoryStore:
    """Open with :func:`open_memory`; use as a context manager."""

    def __init__(self, conn: sqlite3.Connection, root: Path, location: IndexLocation) -> None:
        self.conn = conn
        self.root = root
        self.location = location

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.conn.close()

    # -- writes ------------------------------------------------------------
    def add(self, kind: str, content: str, refs: list[str]) -> MemoryView:
        if kind not in KINDS:
            raise MemoryStoreError(
                f"unknown memory kind {kind!r}; choose one of: {', '.join(KINDS)}"
            )
        content = content.strip()
        if not content:
            raise MemoryStoreError("memory content is empty")
        if len(content) > MAX_CONTENT_CHARS:
            raise MemoryStoreError(f"memory content exceeds {MAX_CONTENT_CHARS} characters")
        if len(refs) > MAX_REFS:
            raise MemoryStoreError(f"at most {MAX_REFS} source references per record")
        verified = [self._verify_new_ref(spec) for spec in refs]
        if kind == "decision":
            provenance = "user-decision"
        elif verified:
            provenance = "source-verified"
        else:
            provenance = "unverified"
        record_id = "mem_" + secrets.token_hex(6)
        now = _now()
        with self._write():
            self.conn.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL)",
                (record_id, kind, content, provenance, read_head_revision(self.root), now, now),
            )
            self.conn.executemany(
                "INSERT INTO refs VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (record_id, i, r.path, r.start_line, r.end_line, r.file_sha256, r.span_sha256)
                    for i, r in enumerate(verified)
                ],
            )
        return self.view(self.get(record_id))

    def invalidate(self, record_id: str, reason: str) -> MemoryView:
        if not reason.strip():
            raise MemoryStoreError("an invalidation reason is required")
        self.get(record_id)  # raises if unknown
        with self._write():
            self.conn.execute(
                "UPDATE records SET status = 'invalidated', invalidated_reason = ?, "
                "updated_at = ? WHERE id = ?",
                (reason.strip(), _now(), record_id),
            )
        return self.view(self.get(record_id))

    def _verify_new_ref(self, spec: str) -> SourceRef:
        match = _REF.match(spec.strip())
        if match is None:
            raise MemoryStoreError(f"invalid reference {spec!r}; expected PATH:START[-END]")
        rel = PurePosixPath(match["path"])
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise MemoryStoreError(f"reference path must be relative to the repository: {spec!r}")
        start = int(match["start"])
        end = int(match["end"] or start)
        if not 1 <= start <= end or end - start + 1 > MAX_SPAN_LINES:
            raise MemoryStoreError(f"invalid line range in {spec!r}")
        data = read_repository_file(self.root, rel, MAX_REF_FILE_BYTES)
        if isinstance(data, str):
            raise MemoryStoreError(f"cannot verify {spec!r}: file is {data}")
        if len(data) > MAX_REF_FILE_BYTES:
            raise MemoryStoreError(f"cannot verify {spec!r}: file is too large")
        lines = _lines(data)
        if end > len(lines):
            raise MemoryStoreError(f"{spec!r} is beyond the end of the file ({len(lines)} lines)")
        return SourceRef(rel.as_posix(), start, end, _sha(data), _span_hash(lines, start, end))

    @contextmanager
    def _write(self) -> Iterator[None]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as exc:
            raise MemoryStoreError(f"memory store error at {self.location.path}: {exc}") from exc

    # -- reads -------------------------------------------------------------
    def get(self, record_id: str) -> MemoryRecord:
        row = self.conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise MemoryStoreError(f"no memory record {record_id!r}")
        refs = tuple(
            SourceRef(r["path"], r["start_line"], r["end_line"], r["file_sha256"], r["span_sha256"])
            for r in self.conn.execute(
                "SELECT * FROM refs WHERE record_id = ? ORDER BY ordinal", (record_id,)
            )
        )
        return MemoryRecord(
            row["id"],
            row["kind"],
            row["content"],
            row["provenance"],
            row["git_revision"],
            row["created_at"],
            row["updated_at"],
            row["status"],
            row["invalidated_reason"],
            refs,
        )

    def records(self, include_invalidated: bool = False) -> list[MemoryRecord]:
        sql = "SELECT id FROM records"
        if not include_invalidated:
            sql += " WHERE status = 'active'"
        return [self.get(r[0]) for r in self.conn.execute(sql + " ORDER BY created_at, id")]

    def views(self, include_invalidated: bool = False) -> list[MemoryView]:
        return [self.view(r) for r in self.records(include_invalidated)]

    def view(self, record: MemoryRecord) -> MemoryView:
        checks = [self._check(ref) for ref in record.refs]
        if record.status == "invalidated":
            status = "invalidated"
        elif record.kind == "decision":
            status = "decision"
        elif not checks:
            status = "unverifiable"
        elif all(c.status == "current" for c in checks):
            status = "current"
        else:
            status = "stale"
        note = ""
        if record.git_revision:
            head = read_head_revision(self.root)
            if head != record.git_revision:
                reachable = is_ancestor(self.root, record.git_revision)
                note = f"created at {record.git_revision[:12]}; HEAD is now {(head or 'none')[:12]}"
                if reachable is False:
                    note += "; that commit is not in HEAD's history (branch switch or reset)"
        return MemoryView(record, status, checks, note)

    def _check(self, ref: SourceRef) -> RefCheck:
        data = read_repository_file(self.root, PurePosixPath(ref.path), MAX_REF_FILE_BYTES)
        if isinstance(data, str):
            if data == "missing":
                moved_to = self._find_renamed(ref)
                if moved_to:
                    return RefCheck(ref, "renamed", f"identical file now at {moved_to}")
                return RefCheck(ref, "missing", "file no longer exists")
            return RefCheck(ref, "replaced", f"path is now {data}")
        if _sha(data) == ref.file_sha256:
            return RefCheck(ref, "current")
        lines = _lines(data)
        if ref.end_line <= len(lines) and _span_hash(lines, ref.start_line, ref.end_line) == (
            ref.span_sha256
        ):
            return RefCheck(ref, "current", "file changed elsewhere; referenced lines unchanged")
        width = ref.end_line - ref.start_line
        for start in range(1, len(lines) - width + 1):
            if _span_hash(lines, start, start + width) == ref.span_sha256:
                return RefCheck(ref, "moved", f"referenced lines now at {start}-{start + width}")
        return RefCheck(ref, "changed", "referenced lines no longer present")

    def _find_renamed(self, ref: SourceRef) -> str | None:
        """An indexed file with the same content hash, read through the safe index opener."""
        from hebra.errors import HebraError
        from hebra.indexing import store
        from hebra.paths import index_location

        try:
            conn = store.open_for_read(index_location(self.root))
        except HebraError:
            return None  # no index, or an unsafe/foreign one: do not consult it
        try:
            row = conn.execute(
                "SELECT path FROM files WHERE sha256 = ? ORDER BY path LIMIT 1",
                (ref.file_sha256,),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if row is None:
            return None
        data = read_repository_file(self.root, PurePosixPath(row[0]), MAX_REF_FILE_BYTES)
        if isinstance(data, bytes) and _sha(data) == ref.file_sha256:
            return str(row[0])
        return None


def open_memory(
    repo_root: Path, db_path: Path | None = None, *, create: bool, writable: bool = False
) -> MemoryStore:
    """Open the memory store for ``repo_root``.

    ``create`` initialises a missing store (and implies ``writable``);
    ``writable`` opens an existing store for updates such as invalidation.
    """
    root = resolve_repository(repo_root)
    location = memory_location(root, db_path)
    prepared = prepare_index(location, create=create)
    if prepared.file_identity is None:
        raise MemoryStoreError(
            f"no memory store at {location.path}; add a record with `hebra memory add`"
        )
    mode = "rw" if create or writable else "ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            location.path.as_uri() + f"?mode={mode}", uri=True, isolation_level=None, timeout=10
        )
        conn.row_factory = sqlite3.Row
        prepared.verify_unchanged()
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        objects = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        identity = repository_identity(root)
        if objects == 0 and create:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA application_id = {APPLICATION_ID:d}")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
            conn.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [("identity", json.dumps(identity, sort_keys=True)), ("created_at", _now())],
            )
            conn.execute("COMMIT")
        elif app_id != APPLICATION_ID:
            raise MemoryStoreError(
                f"{location.path} is not a Hebra memory store; refusing to use it"
            )
        stored = json.loads(
            conn.execute("SELECT value FROM meta WHERE key = 'identity'").fetchone()[0]
        )
    except sqlite3.DatabaseError as exc:
        if conn is not None:
            conn.close()
        raise MemoryStoreError(f"memory store at {location.path} is unusable: {exc}") from exc
    except HebraError:
        if conn is not None:
            conn.close()
        raise
    if stored != identity:
        conn.close()
        raise MemoryIdentityError(
            f"memory store {location.path} belongs to a different repository "
            f"({stored}); this repository is {identity}"
        )
    return MemoryStore(conn, root, location)
