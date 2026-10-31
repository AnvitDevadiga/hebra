"""Lexical BM25 retrieval over indexed chunks using SQLite FTS5.

This is keyword matching, not semantic search: a chunk scores only if it
shares terms with the task text. Scores are FTS5 ``bm25()`` values negated so
that higher is better; they are comparable within one query only.

Freshness: results are served from the index snapshot, so every result's
source file is re-checked (size and SHA-256, read without following
symlinks) against the working tree. Each hit carries ``source_status``:
"current", "modified", "missing", "replaced" (now a symlink or special file),
or "unreadable". Only files behind returned hits are read, at most once each.
A file can still change after it is checked; the check is a point-in-time
snapshot, not a lock.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from hebra.errors import QueryError
from hebra.indexing import store
from hebra.indexing.gitmeta import read_head_revision
from hebra.indexing.scanner import read_repository_file
from hebra.paths import index_location, resolve_repository
from hebra.terms import MAX_QUERY_TERMS, query_terms, word_set

# Column weights for bm25(chunks_fts, path, symbols, identifiers, body).
# These are untuned starting values: symbol and path matches count for more
# than matches in the body text.
BM25_WEIGHTS = (2.0, 3.0, 1.0, 1.0)
DEFAULT_LIMIT = 10
MAX_LIMIT = 1_000

_SEARCH_SQL = """
SELECT c.id, f.path, f.language, c.start_line, c.end_line, c.kind, c.symbol,
       c.content, c.content_sha256, f.sha256 AS file_sha256, f.size_bytes,
       fts.path AS fts_path, fts.symbols AS fts_symbols, fts.identifiers AS fts_identifiers,
       bm25(chunks_fts, ?, ?, ?, ?) AS bm25_score
FROM chunks_fts AS fts
JOIN chunks AS c ON c.id = fts.rowid
JOIN files AS f ON f.id = c.file_id
WHERE chunks_fts MATCH ?
ORDER BY bm25_score ASC, f.path ASC, c.start_line ASC
LIMIT ?
"""


@dataclass(frozen=True)
class SearchHit:
    rank: int
    path: str
    start_line: int
    end_line: int
    kind: str
    symbol: str | None
    language: str
    score: float
    matched_terms: list[str]
    content: str
    content_sha256: str
    source_status: str  # see module docstring

    def to_dict(self, include_content: bool = False) -> dict[str, object]:
        data = asdict(self)
        if not include_content:
            del data["content"]
        return data


@dataclass
class SearchResponse:
    task: str
    terms: list[str]
    index_path: str
    index_revision: str | None
    current_revision: str | None
    hits: list[SearchHit]
    warnings: list[str] = field(default_factory=list)


def build_match_expression(terms: list[str]) -> str:
    """Build an FTS5 OR-query from terms.

    Each term is wrapped in double quotes so FTS5 treats it as a literal
    string; terms come from ``query_terms`` and contain only letters and
    digits, so they can never close the quote or inject query syntax.
    """
    for term in terms:
        if not term or not all(ch.isalnum() for ch in term):
            raise QueryError(f"invalid search term: {term!r}")
    return " OR ".join(f'"{term}"' for term in terms)


def _staleness_warning(index_revision: str | None, current: str | None) -> list[str]:
    if index_revision and current and index_revision != current:
        return [
            f"index was built at revision {index_revision[:12]} but HEAD is now "
            f"{current[:12]}; run `hebra index` to refresh"
        ]
    return []


def source_status(root: Path, path: str, sha256: str, size_bytes: int) -> str:
    """Compare one indexed file with the working tree (see module docstring)."""
    data = read_repository_file(root, PurePosixPath(path), size_bytes)
    if isinstance(data, str):
        return {"missing": "missing", "unreadable": "unreadable"}.get(data, "replaced")
    if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha256:
        return "modified"
    return "current"


def search(
    repo_root: Path,
    task: str,
    limit: int = DEFAULT_LIMIT,
    db_path: Path | None = None,
) -> SearchResponse:
    """Rank indexed chunks against ``task`` with BM25."""
    if not task or not task.strip():
        raise QueryError("task text is empty")
    if not 1 <= limit <= MAX_LIMIT:
        raise QueryError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")
    all_terms = query_terms(task)
    if not all_terms:
        raise QueryError(
            "task text contains no searchable terms after removing punctuation and stop words"
        )
    warnings: list[str] = []
    terms = all_terms[:MAX_QUERY_TERMS]
    if len(all_terms) > MAX_QUERY_TERMS:
        warnings.append(
            f"task has {len(all_terms)} distinct search terms; only the first "
            f"{MAX_QUERY_TERMS} were used ({', '.join(all_terms[MAX_QUERY_TERMS:][:5])}, ... "
            "were ignored)"
        )

    root = resolve_repository(repo_root)
    location = index_location(root, db_path)
    conn = store.open_for_read(location)
    try:
        with store.database_errors(location.path):
            meta = store.get_meta(conn)
            rows = conn.execute(
                _SEARCH_SQL, (*BM25_WEIGHTS, build_match_expression(terms), limit)
            ).fetchall()
    finally:
        conn.close()

    scored = []
    for row in rows:
        words = word_set(
            " ".join((row["fts_path"], row["fts_symbols"], row["fts_identifiers"], row["content"]))
        )
        scored.append((round(-row["bm25_score"], 6), row, [t for t in terms if t in words]))
    # Re-sort on the rounded score so float noise cannot reorder ties.
    scored.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["start_line"]))

    statuses: dict[str, str] = {}
    for _, row, _ in scored:
        if row["path"] not in statuses:
            statuses[row["path"]] = source_status(
                root, row["path"], row["file_sha256"], row["size_bytes"]
            )

    hits = [
        SearchHit(
            rank=i,
            path=row["path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            kind=row["kind"],
            symbol=row["symbol"],
            language=row["language"],
            score=score,
            matched_terms=matched,
            content=row["content"],
            content_sha256=row["content_sha256"],
            source_status=statuses[row["path"]],
        )
        for i, (score, row, matched) in enumerate(scored, start=1)
    ]
    stale = sorted({h.path for h in hits if h.source_status != "current"})
    if stale:
        warnings.append(
            f"{len(stale)} file(s) behind these results changed since indexing "
            f"(e.g. {stale[0]}); their content and line numbers are not current. Run "
            "`hebra index` to refresh"
        )
    index_revision = meta.get("head_revision") or None
    current_revision = read_head_revision(root)
    return SearchResponse(
        task=task,
        terms=terms,
        index_path=str(location.path),
        index_revision=index_revision,
        current_revision=current_revision,
        hits=hits,
        warnings=_staleness_warning(index_revision, current_revision) + warnings,
    )
