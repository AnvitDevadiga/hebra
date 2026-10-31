"""Bounded, deterministic traversal of the source graph."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

MAX_HOPS = 2
DEFAULT_MAX_NODES = 50
EXPANSION_KINDS = ("calls", "references", "tests", "contains")


@dataclass(frozen=True)
class Neighbor:
    node: str
    hops: int
    seed: str  # the seed this node was first reached from
    edge_kind: str  # kind of the edge that reached it
    direction: str  # "out" (seed -> node) or "in" (node -> seed)


def neighbors(
    conn: sqlite3.Connection,
    seeds: list[str],
    *,
    max_hops: int = 1,
    max_nodes: int = DEFAULT_MAX_NODES,
    kinds: tuple[str, ...] = EXPANSION_KINDS,
) -> list[Neighbor]:
    """Nodes within ``max_hops`` resolved edges of ``seeds`` (seeds excluded).

    Breadth-first in seed order; within a hop, edges are visited in
    (kind, direction, node id) order. Stops once ``max_nodes`` nodes are
    found, so work is bounded regardless of graph density. ``contains``
    edges from file nodes are not followed (a file contains everything in it).
    """
    if not 0 <= max_hops <= MAX_HOPS:
        raise ValueError(f"max_hops must be between 0 and {MAX_HOPS}")
    placeholders = ",".join("?" * len(kinds))
    out_sql = (
        f"SELECT kind, dst FROM edges WHERE src = ? AND status = 'resolved' "
        f"AND kind IN ({placeholders}) AND dst IS NOT NULL ORDER BY kind, dst"
    )
    in_sql = (
        f"SELECT kind, src FROM edges WHERE dst = ? AND status = 'resolved' "
        f"AND kind IN ({placeholders}) ORDER BY kind, src"
    )
    found: dict[str, Neighbor] = {}
    visited = set(seeds)
    frontier = [(seed, seed) for seed in seeds]
    for hop in range(1, max_hops + 1):
        next_frontier: list[tuple[str, str]] = []
        for node, seed in frontier:
            steps: list[tuple[str, str, str]] = []
            if not node.startswith("file:"):
                steps += [(k, "out", d) for k, d in conn.execute(out_sql, (node, *kinds))]
            steps += [(k, "in", s) for k, s in conn.execute(in_sql, (node, *kinds))]
            for kind, direction, other in sorted(steps):
                if other in visited or other.startswith("file:"):
                    continue
                visited.add(other)
                found[other] = Neighbor(other, hop, seed, kind, direction)
                next_frontier.append((other, seed))
                if len(found) >= max_nodes:
                    return list(found.values())
        frontier = next_frontier
    return list(found.values())


@dataclass(frozen=True)
class SymbolInfo:
    symbol_id: str
    path: str
    qualname: str
    kind: str
    start_line: int
    end_line: int


@dataclass
class Exploration:
    symbol: SymbolInfo
    outgoing: list[tuple[str, str, str, str, int]]  # kind, status, target/dst, detail, line
    incoming: list[tuple[str, str, int]]  # kind, src, line
    neighborhood: list[Neighbor]

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol.__dict__,
            "outgoing": [
                {"kind": k, "status": st, "target": t, "detail": d, "line": ln}
                for k, st, t, d, ln in self.outgoing
            ],
            "incoming": [{"kind": k, "src": s, "line": ln} for k, s, ln in self.incoming],
            "neighborhood": [n.__dict__ for n in self.neighborhood],
        }


def find_symbols(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[SymbolInfo]:
    """Symbols matching an id, qualname, or bare name, in a stable order."""
    rows = conn.execute(
        "SELECT s.symbol_id, f.path, s.qualname, s.kind, s.start_line, s.end_line "
        "FROM symbols s JOIN files f ON f.id = s.file_id "
        "WHERE s.symbol_id = ? OR s.qualname = ? OR s.name = ? "
        "ORDER BY (s.symbol_id = ?) DESC, (s.qualname = ?) DESC, f.path, s.start_line LIMIT ?",
        (query, query, query, query, query, limit),
    ).fetchall()
    return [SymbolInfo(*row) for row in rows]


def explore(
    conn: sqlite3.Connection, symbol: SymbolInfo, *, max_hops: int = 1, max_nodes: int = 25
) -> Exploration:
    outgoing = [
        (k, st, dst or tgt, d, ln)
        for k, st, dst, tgt, d, ln in conn.execute(
            "SELECT kind, status, dst, target, detail, line FROM edges WHERE src = ? "
            "ORDER BY status != 'resolved', kind, COALESCE(dst, target)",
            (symbol.symbol_id,),
        )
    ]
    incoming = [
        (k, src, ln)
        for k, src, ln in conn.execute(
            "SELECT kind, src, line FROM edges WHERE dst = ? AND status = 'resolved' "
            "ORDER BY kind, src",
            (symbol.symbol_id,),
        )
    ]
    found = neighbors(conn, [symbol.symbol_id], max_hops=max_hops, max_nodes=max_nodes)
    return Exploration(symbol, outgoing, incoming, found)
