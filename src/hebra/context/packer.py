"""Token-budgeted context packing.

Pipeline:

1. Retrieve up to ``candidates`` fragments (``bm25`` or ``bm25_graph``).
2. Exclude candidates whose source file no longer matches the indexed
   snapshot (``SearchHit.source_status != "current"``) and exact duplicates.
3. Optionally add verified repository memory records as candidates.
4. Select with ``greedy`` (Phase 1 policy) or ``optimized`` (see
   :mod:`hebra.context.optimizer`). Every acceptance re-counts the complete
   rendered document with the real tokenizer, so the budget guarantee covers
   all formatting overhead and token merges across fragment boundaries.
5. Verify the final document against the budget before returning it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hebra import __version__
from hebra.context.optimizer import STRATEGIES, Decision, Item, Objective, Weights, select
from hebra.context.render import (
    render_fragment,
    render_header,
    render_memory,
    selection_reason,
)
from hebra.context.tokenizer import DEFAULT_ENCODING, SUPPORTED_ENCODINGS, get_token_counter
from hebra.errors import BudgetError
from hebra.paths import resolve_repository
from hebra.retrieval.search import MODES, SearchHit, search

if TYPE_CHECKING:
    from hebra.memory import MemoryView

DEFAULT_CANDIDATES = 50
MAX_BUDGET = 2_000_000
PACK_SCHEMA = "hebra.pack/v2"


@dataclass(frozen=True)
class Fragment:
    hit: SearchHit
    tokens: int  # exact marginal tokens this fragment added to the document
    reason: str


@dataclass(frozen=True)
class Exclusion:
    hit: SearchHit
    reason: str


@dataclass(frozen=True)
class MemoryFragment:
    view: MemoryView
    tokens: int


@dataclass
class PackResult:
    task: str
    repo_root: str
    index_path: str
    index_revision: str | None
    current_revision: str | None
    encoding: str
    budget: int
    header_tokens: int
    used_tokens: int
    candidates: int
    files_checked: int
    stale_excluded: int
    terms: list[str]
    selected: list[Fragment]
    excluded: list[Exclusion]
    markdown: str
    warnings: list[str] = field(default_factory=list)
    retrieval: str = "bm25"
    strategy: str = "greedy"
    weights: Weights = field(default_factory=Weights)
    utility: float = 0.0
    selection_note: str = ""
    trace: list[Decision] = field(default_factory=list)
    memory: list[MemoryFragment] = field(default_factory=list)
    memory_excluded: list[tuple[str, str]] = field(default_factory=list)  # (id, reason)

    def to_dict(self) -> dict[str, Any]:
        def ref(hit: SearchHit) -> dict[str, Any]:
            return {
                "rank": hit.rank,
                "path": hit.path,
                "start_line": hit.start_line,
                "end_line": hit.end_line,
                "kind": hit.kind,
                "symbol": hit.symbol,
                "score": hit.score,
                "content_sha256": hit.content_sha256,
                "source_status": hit.source_status,
                "retrieval": {
                    "sources": list(hit.sources),
                    "bm25_rank": hit.bm25_rank,
                    "bm25_score": hit.bm25_score,
                    "graph_rank": hit.graph_rank,
                    "evidence": list(hit.evidence),
                },
            }

        return {
            "schema": PACK_SCHEMA,
            "hebra_version": __version__,
            "task": self.task,
            "query_terms": self.terms,
            "repository": {
                "root": self.repo_root,
                "index_path": self.index_path,
                "index_revision": self.index_revision,
                "current_revision": self.current_revision,
            },
            "tokenizer": {
                "library": "tiktoken",
                "encoding": self.encoding,
                "note": SUPPORTED_ENCODINGS[self.encoding]
                + "; other model tokenizers will produce different counts",
            },
            "method": {
                "retrieval": self.retrieval,
                "strategy": self.strategy,
                "objective_weights": {
                    "alpha": self.weights.alpha,
                    "beta": self.weights.beta,
                    "gamma": self.weights.gamma,
                },
                "utility": round(self.utility, 6),
                "note": self.selection_note,
            },
            "budget": {
                "limit": self.budget,
                "used": self.used_tokens,
                "remaining": self.budget - self.used_tokens,
                "header_tokens": self.header_tokens,
                "fragment_tokens": self.used_tokens - self.header_tokens,
                "utilization": round(self.used_tokens / self.budget, 6),
            },
            "candidates_considered": self.candidates,
            "source_check": {
                "policy": "exclude-stale",
                "files_checked": self.files_checked,
                "stale_candidates_excluded": self.stale_excluded,
            },
            "selected": [
                {
                    **ref(f.hit),
                    "tokens": f.tokens,
                    "matched_terms": f.hit.matched_terms,
                    "reason": f.reason,
                }
                for f in self.selected
            ],
            "excluded": [{**ref(e.hit), "reason": e.reason} for e in self.excluded],
            "memory": {
                "selected": [{**m.view.to_dict(), "tokens": m.tokens} for m in self.memory],
                "excluded": [{"id": i, "reason": r} for i, r in self.memory_excluded],
            },
            "trace": [
                {
                    "key": d.key,
                    "rank": d.rank,
                    "selected": d.selected,
                    "reason": d.reason,
                    "tokens": d.tokens,
                    "standalone_gain": round(d.standalone_gain, 6),
                    "marginal_gain": None if d.marginal_gain is None else round(d.marginal_gain, 6),
                    "redundancy": None if d.redundancy is None else round(d.redundancy, 6),
                    "gain_per_token": None if d.ratio is None else round(d.ratio, 9),
                }
                for d in self.trace
            ],
            "warnings": self.warnings,
            "context": self.markdown,
        }


def validate_budget(budget: int) -> None:
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise BudgetError(f"budget must be an integer, got {budget!r}")
    if budget <= 0:
        raise BudgetError(f"budget must be a positive number of tokens, got {budget}")
    if budget > MAX_BUDGET:
        raise BudgetError(f"budget must be at most {MAX_BUDGET} tokens, got {budget}")


def chunk_key(hit: SearchHit) -> str:
    return f"chunk:{hit.path}:{hit.start_line}"


def pack_context(
    repo_root: Path,
    task: str,
    budget: int,
    *,
    encoding: str = DEFAULT_ENCODING,
    candidates: int = DEFAULT_CANDIDATES,
    db_path: Path | None = None,
    retrieval: str = "bm25",
    strategy: str = "greedy",
    weights: Weights | None = None,
    memory: list[MemoryView] | None = None,
) -> PackResult:
    """Select fragments for ``task`` whose rendered Markdown fits ``budget``.

    ``memory`` is an optional list of repository memory records (already
    checked for freshness by :mod:`hebra.memory`); only records whose status
    is ``current`` or ``decision`` and that share a query term are eligible.
    """
    validate_budget(budget)
    if strategy not in STRATEGIES:
        raise BudgetError(f"unknown strategy {strategy!r}; choose one of: {', '.join(STRATEGIES)}")
    if retrieval not in MODES:
        raise BudgetError(f"unknown retrieval {retrieval!r}; choose one of: {', '.join(MODES)}")
    weights = weights or Weights()
    counter = get_token_counter(encoding)
    root = resolve_repository(repo_root)
    response = search(root, task, limit=candidates, db_path=db_path, mode=retrieval)

    files_checked = len({hit.path for hit in response.hits})
    stale_count = sum(1 for hit in response.hits if hit.source_status != "current")
    source_check = (
        f"{files_checked} candidate file(s) verified against the working tree; "
        f"{stale_count} stale candidate(s) excluded"
    )
    document = render_header(
        task,
        root.name,
        response.index_revision,
        budget,
        encoding,
        source_check,
        f"{retrieval} retrieval, {strategy} selection",
    )
    header_tokens = counter.count(document)
    if header_tokens > budget:
        raise BudgetError(
            f"budget of {budget} tokens is too small: the context header alone needs "
            f"{header_tokens} tokens ({encoding})"
        )

    excluded: list[Exclusion] = []
    pre_excluded: list[Decision] = []  # rejected before selection (stale, duplicate)
    seen_content: dict[str, SearchHit] = {}
    hits_by_key: dict[str, SearchHit] = {}
    reasons: dict[str, str] = {}
    items: list[Item] = []
    total = len(response.hits)
    for hit in response.hits:
        if hit.source_status != "current":
            reason = (
                f"source file is {hit.source_status} since indexing, so its content and "
                "line numbers are not current; re-run `hebra index`"
            )
            excluded.append(Exclusion(hit, reason))
            pre_excluded.append(Decision(chunk_key(hit), hit.rank, False, reason))
            continue
        duplicate_of = seen_content.get(hit.content_sha256)
        if duplicate_of is not None:
            reason = (
                f"identical content already selected from {duplicate_of.path}:"
                f"{duplicate_of.start_line}-{duplicate_of.end_line}"
            )
            excluded.append(Exclusion(hit, reason))
            pre_excluded.append(Decision(chunk_key(hit), hit.rank, False, reason))
            continue
        seen_content[hit.content_sha256] = hit
        key = chunk_key(hit)
        hits_by_key[key] = hit
        reasons[key] = selection_reason(hit, total, retrieval)
        items.append(Item(key, hit.rank, frozenset(hit.matched_terms), frozenset(hit.symbols)))

    memory_by_key: dict[str, MemoryView] = {}
    memory_excluded: list[tuple[str, str]] = []
    terms = set(response.terms)
    for offset, view in enumerate(memory or [], start=1):
        overlap = frozenset(terms & view.terms)
        if view.status not in ("current", "decision"):
            memory_excluded.append((view.record.id, f"memory status is {view.status}"))
        elif not overlap:
            memory_excluded.append((view.record.id, "shares no term with the task"))
        else:
            key = f"memory:{view.record.id}"
            memory_by_key[key] = view
            # Memory ranks after every code candidate (a conservative prior).
            items.append(Item(key, total + offset, overlap, frozenset()))

    def render(item: Item, position: int) -> str:
        if item.key in memory_by_key:
            return render_memory(position, memory_by_key[item.key])
        return render_fragment(position, hits_by_key[item.key], reasons[item.key])

    objective = Objective(items, response.term_weights, response.symbol_weights, weights)
    chosen = select(strategy, items, objective, document, budget, render, counter.count)

    final_tokens = counter.count(chosen.document)
    if final_tokens != chosen.used_tokens or final_tokens > budget:  # pragma: no cover
        raise AssertionError(
            f"internal error: packed context has {final_tokens} tokens, accounted "
            f"{chosen.used_tokens}, budget {budget}"
        )

    selected: list[Fragment] = []
    memory_selected: list[MemoryFragment] = []
    for item in chosen.order:
        decision = chosen.decisions[item.key]
        assert decision.tokens is not None
        if item.key in memory_by_key:
            memory_selected.append(MemoryFragment(memory_by_key[item.key], decision.tokens))
        else:
            selected.append(Fragment(hits_by_key[item.key], decision.tokens, reasons[item.key]))
    chosen_keys = {item.key for item in chosen.order}
    for item in items:
        if item.key in chosen_keys:
            continue
        reason = chosen.decisions[item.key].reason
        if item.key in memory_by_key:
            memory_excluded.append((memory_by_key[item.key].record.id, reason))
        else:
            excluded.append(Exclusion(hits_by_key[item.key], reason))
    excluded.sort(key=lambda e: e.hit.rank)

    warnings = list(response.warnings)
    if not response.hits:
        warnings.append("no indexed fragments matched the task terms")
    elif not selected and stale_count == total:
        warnings.append("all matching fragments come from files changed since indexing")
    elif not selected:
        warnings.append("matching fragments were found but none fit within the budget")

    return PackResult(
        task=task,
        repo_root=str(root),
        index_path=response.index_path,
        index_revision=response.index_revision,
        current_revision=response.current_revision,
        encoding=encoding,
        budget=budget,
        header_tokens=header_tokens,
        used_tokens=chosen.used_tokens,
        candidates=total,
        files_checked=files_checked,
        stale_excluded=stale_count,
        terms=response.terms,
        selected=selected,
        excluded=excluded,
        markdown=chosen.document,
        warnings=warnings,
        retrieval=retrieval,
        strategy=strategy,
        weights=weights,
        utility=chosen.utility,
        selection_note=chosen.note,
        trace=sorted([*chosen.decisions.values(), *pre_excluded], key=lambda d: (d.rank, d.key)),
        memory=memory_selected,
        memory_excluded=memory_excluded,
    )
