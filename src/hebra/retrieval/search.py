"""Retrieval over indexed chunks: lexical BM25, optionally expanded through the graph.

Modes:

``bm25``        SQLite FTS5 ``bm25()`` keyword ranking (the Phase 1 baseline).
                ``score`` is the negated FTS5 value, higher is better, and is
                comparable within one query only.
``bm25_graph``  BM25 plus bounded expansion through statically resolved graph
                edges (calls, references, tests, contains), merged with
                reciprocal rank fusion. ``score`` is the fused RRF score.

Neither mode is semantic search: nothing here understands meaning, and graph
expansion only follows relationships that resolve statically.

Reciprocal rank fusion (Cormack et al., 2009): with ranked lists ``L`` and
``rank_L(c)`` the 1-based rank of candidate ``c`` in ``L``,
``rrf(c) = sum over L containing c of 1 / (K + rank_L(c))`` with ``K = 60``.
Only ranks are combined, never raw scores from different methods.

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
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from hebra.errors import QueryError
from hebra.graph.build import is_test_file
from hebra.graph.traverse import EDGE_PRIORITY, neighbors
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
MODES = ("bm25", "bm25_graph")
RRF_K = 60
# Graph expansion bounds: seeds come from the top BM25 hits only, expansion
# follows at most GRAPH_HOPS resolved edges, and stops at GRAPH_MAX_NODES.
GRAPH_SEED_HITS = 10
GRAPH_HOPS = 1
GRAPH_MAX_NODES = 40
# BM25 candidate pool used before fusion in bm25_graph mode.
BM25_POOL = 50

_COLUMNS = """
c.id, f.path, f.language, c.start_line, c.end_line, c.kind, c.symbol,
c.content, c.content_sha256, f.sha256 AS file_sha256, f.size_bytes,
fts.path AS fts_path, fts.symbols AS fts_symbols, fts.identifiers AS fts_identifiers
"""

_SEARCH_SQL = f"""
SELECT {_COLUMNS}, bm25(chunks_fts, ?, ?, ?, ?) AS bm25_score
FROM chunks_fts AS fts
JOIN chunks AS c ON c.id = fts.rowid
JOIN files AS f ON f.id = c.file_id
WHERE chunks_fts MATCH ?
ORDER BY bm25_score ASC, f.path ASC, c.start_line ASC
LIMIT ?
"""

_CHUNK_SQL = f"""
SELECT {_COLUMNS}, NULL AS bm25_score
FROM chunks AS c
JOIN chunks_fts AS fts ON fts.rowid = c.id
JOIN files AS f ON f.id = c.file_id
WHERE c.id = ?
"""

_DEFINING_CHUNK_SQL = """
SELECT c.id FROM chunks AS c JOIN files AS f ON f.id = c.file_id
WHERE f.path = ? AND c.start_line <= ? AND c.end_line >= ?
ORDER BY c.start_line, c.id LIMIT 1
"""

_SYMBOLS_IN_SPAN_SQL = """
SELECT s.symbol_id FROM symbols AS s JOIN files AS f ON f.id = s.file_id
WHERE f.path = ? AND s.start_line BETWEEN ? AND ?
ORDER BY s.start_line, s.symbol_id
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
    score: float  # BM25 score (bm25 mode) or fused RRF score (bm25_graph mode)
    matched_terms: list[str]
    content: str
    content_sha256: str
    source_status: str  # see module docstring
    bm25_rank: int | None = None  # rank in the BM25 list, if it appeared there
    bm25_score: float | None = None
    graph_rank: int | None = None  # rank in the graph-expansion list, if any
    sources: tuple[str, ...] = ("bm25",)
    evidence: tuple[str, ...] = ()  # graph edges that led to this chunk
    symbols: tuple[str, ...] = ()  # graph-relevant symbols defined in this chunk

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
    mode: str = "bm25"
    # Inverse document frequency of each query term over all indexed chunks.
    term_weights: dict[str, float] = field(default_factory=dict)
    # Graph relevance of symbols reached in bm25_graph mode (seeds and neighbors).
    symbol_weights: dict[str, float] = field(default_factory=dict)


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


def _term_weights(conn: sqlite3.Connection, terms: list[str]) -> dict[str, float]:
    """BM25-style IDF, ``ln(1 + (N - df + 0.5) / (df + 0.5))``, always positive."""
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    weights = {}
    for term in terms:
        df = conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
            (build_match_expression([term]),),
        ).fetchone()[0]
        weights[term] = math.log(1 + (total - df + 0.5) / (df + 0.5))
    return weights


@dataclass
class _Candidate:
    row: sqlite3.Row
    bm25_rank: int | None = None
    bm25_score: float | None = None
    graph_rank: int | None = None
    evidence: list[str] = field(default_factory=list)


def _graph_expand(
    conn: sqlite3.Connection, bm25: list[_Candidate]
) -> tuple[list[_Candidate], dict[str, float]]:
    """The graph-expansion ranked list: the top BM25 hits (hop 0), then neighbors.

    Seeds are the symbols defined in the top ``GRAPH_SEED_HITS`` BM25 hits,
    weighted ``1 / bm25_rank``. Neighbors are ordered by hop count, the BM25
    rank of the seed that reached them, ``EDGE_PRIORITY``, and whether they
    are test code (source first), and weighted ``seed weight * 0.5 ** hops``.
    """
    seeds: list[str] = []
    weights: dict[str, float] = {}
    by_chunk: dict[int, _Candidate] = {}
    for cand in bm25[:GRAPH_SEED_HITS]:
        row = cand.row
        defined = [
            sid
            for (sid,) in conn.execute(
                _SYMBOLS_IN_SPAN_SQL, (row["path"], row["start_line"], row["end_line"])
            )
        ]
        for sid in defined:
            if sid not in weights:
                seeds.append(sid)
                weights[sid] = 1.0 / (cand.bm25_rank or 1)
        # Every top BM25 hit enters the graph list at hop 0, so fusion does
        # not demote hits that define no symbol (module headers, docs).
        by_chunk[row["id"]] = _Candidate(
            row, evidence=[f"seed: {defined[0]}" if defined else "seed: top BM25 hit"]
        )

    found = neighbors(conn, seeds, max_hops=GRAPH_HOPS, max_nodes=GRAPH_MAX_NODES)
    order = {sid: i for i, sid in enumerate(seeds)}
    found.sort(
        key=lambda n: (
            n.hops,
            order[n.seed],
            EDGE_PRIORITY.get((n.edge_kind, n.direction), 9),
            is_test_file(n.node.partition("::")[0]),
            n.node,
        )
    )
    for n in found:
        weights.setdefault(n.node, weights[n.seed] * 0.5**n.hops)
        path = n.node.partition("::")[0]
        line_row = conn.execute(
            "SELECT start_line FROM symbols WHERE symbol_id = ?", (n.node,)
        ).fetchone()
        if line_row is None:
            continue
        chunk = conn.execute(_DEFINING_CHUNK_SQL, (path, line_row[0], line_row[0])).fetchone()
        if chunk is None:
            continue
        arrow = "->" if n.direction == "out" else "<-"
        note = f"{n.seed} {arrow} {n.edge_kind} {n.node}"
        if chunk[0] in by_chunk:
            by_chunk[chunk[0]].evidence.append(note)
            continue
        by_chunk[chunk[0]] = _Candidate(
            conn.execute(_CHUNK_SQL, (chunk[0],)).fetchone(), evidence=[note]
        )
    graph = list(by_chunk.values())
    for rank, cand in enumerate(graph, start=1):
        cand.graph_rank = rank
    return graph, weights


def search(
    repo_root: Path,
    task: str,
    limit: int = DEFAULT_LIMIT,
    db_path: Path | None = None,
    mode: str = "bm25",
) -> SearchResponse:
    """Rank indexed chunks against ``task`` (see module docstring for modes)."""
    if mode not in MODES:
        raise QueryError(f"unknown retrieval mode {mode!r}; choose one of: {', '.join(MODES)}")
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
    symbol_weights: dict[str, float] = {}
    try:
        with store.database_errors(location.path):
            meta = store.get_meta(conn)
            pool = limit if mode == "bm25" else max(limit, BM25_POOL)
            rows = conn.execute(
                _SEARCH_SQL, (*BM25_WEIGHTS, build_match_expression(terms), pool)
            ).fetchall()
            bm25 = [_Candidate(row, bm25_score=round(-row["bm25_score"], 6)) for row in rows]
            # Re-sort on the rounded score so float noise cannot reorder ties.
            bm25.sort(key=lambda c: (-(c.bm25_score or 0.0), c.row["path"], c.row["start_line"]))
            for rank, cand in enumerate(bm25, start=1):
                cand.bm25_rank = rank
            if mode == "bm25":
                ranked = [(c.bm25_score or 0.0, c) for c in bm25]
            else:
                graph, symbol_weights = _graph_expand(conn, bm25)
                ranked = _fuse(bm25, graph)
            ranked = ranked[:limit]
            chunk_symbols = {
                id(c): tuple(
                    sid
                    for (sid,) in conn.execute(
                        _SYMBOLS_IN_SPAN_SQL,
                        (c.row["path"], c.row["start_line"], c.row["end_line"]),
                    )
                    if sid in symbol_weights
                )
                for _, c in ranked
            }
            term_weights = _term_weights(conn, terms)
    finally:
        conn.close()

    statuses: dict[str, str] = {}
    for _, cand in ranked:
        row = cand.row
        if row["path"] not in statuses:
            statuses[row["path"]] = source_status(
                root, row["path"], row["file_sha256"], row["size_bytes"]
            )

    hits = []
    for i, (score, cand) in enumerate(ranked, start=1):
        row = cand.row
        words = word_set(
            " ".join((row["fts_path"], row["fts_symbols"], row["fts_identifiers"], row["content"]))
        )
        hits.append(
            SearchHit(
                rank=i,
                path=row["path"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                kind=row["kind"],
                symbol=row["symbol"],
                language=row["language"],
                score=score,
                matched_terms=[t for t in terms if t in words],
                content=row["content"],
                content_sha256=row["content_sha256"],
                source_status=statuses[row["path"]],
                bm25_rank=cand.bm25_rank,
                bm25_score=cand.bm25_score,
                graph_rank=cand.graph_rank,
                sources=tuple(
                    s for s, r in (("bm25", cand.bm25_rank), ("graph", cand.graph_rank)) if r
                ),
                evidence=tuple(cand.evidence[:5]),
                symbols=chunk_symbols[id(cand)],
            )
        )
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
        mode=mode,
        term_weights=term_weights,
        symbol_weights=symbol_weights,
    )


def _fuse(bm25: list[_Candidate], graph: list[_Candidate]) -> list[tuple[float, _Candidate]]:
    """Reciprocal rank fusion of the BM25 and graph lists, deterministic ties."""
    merged: dict[int, _Candidate] = {c.row["id"]: c for c in bm25}
    for cand in graph:
        existing = merged.get(cand.row["id"])
        if existing is None:
            merged[cand.row["id"]] = cand
        else:
            existing.graph_rank = cand.graph_rank
            existing.evidence.extend(cand.evidence)
    scored = []
    for cand in merged.values():
        score = sum(1.0 / (RRF_K + r) for r in (cand.bm25_rank, cand.graph_rank) if r)
        scored.append((round(score, 9), cand))
    scored.sort(key=lambda item: (-item[0], item[1].row["path"], item[1].row["start_line"]))
    return scored
