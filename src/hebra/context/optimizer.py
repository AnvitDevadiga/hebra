"""Fragment selection under an exact token budget.

Two strategies share the same exact feasibility check (the whole rendered
document is re-counted with the real tokenizer before any fragment is
accepted), so both inherit the budget guarantee. See ``docs/optimizer.md``
for the objective, its properties, and what is heuristic.

``greedy``     Phase 1 policy: walk candidates in retrieval rank order and
               keep each one that still fits.
``optimized``  Cost-benefit greedy on a monotone submodular utility, plus the
               best single fragment; the better of the two is returned.

Utility of a selected set ``S``::

    U(S) = alpha * sum_{f in S} r_f
         + beta  * sum_{t in T} w_t * [t matched by some f in S]
         + gamma * sum_{s in G} g_s * [s defined in some f in S]

``r_f`` is a rank prior ``rank ** -k`` with ``k = Weights.rank_decay``;
``w_t`` are query-term IDF weights and ``g_s`` graph-symbol weights, each
normalised to sum to 1. Coverage terms make repeated evidence worth less:
the *redundancy* reported for a fragment is its stand-alone gain minus its
marginal gain at the moment it was selected. ``U`` is a heuristic score, not
a probability of usefulness.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

STRATEGIES = ("greedy", "optimized")
# Skip the exact re-count when a fragment's stand-alone size exceeds the
# remaining budget by more than this; acceptance always uses the exact count.
BOUNDARY_SLACK_TOKENS = 16


@dataclass(frozen=True)
class Weights:
    """Objective weights. Defaults chosen on the benchmark's dev split only
    (``benchmarks/results/v1-dev-tuning``); every grid setting scored within
    0.015 mean symbol recall of the others, so the choice is not significant."""

    alpha: float = 1.0  # rank prior
    beta: float = 1.0  # query-term coverage
    gamma: float = 0.5  # graph-symbol coverage
    rank_decay: float = 0.0  # k in the rank prior rank ** -k (0 = flat)

    def __post_init__(self) -> None:
        if min(self.alpha, self.beta, self.gamma, self.rank_decay) < 0:
            raise ValueError("objective weights must be non-negative")


@dataclass(frozen=True)
class Item:
    key: str  # unique and stable, e.g. "chunk:path:start" or "memory:id"
    rank: int  # 1-based rank among candidates, used for the prior and ties
    terms: frozenset[str]
    symbols: frozenset[str]


@dataclass
class Decision:
    key: str
    rank: int
    selected: bool
    reason: str
    tokens: int | None = None  # exact tokens added to the final document
    standalone_gain: float = 0.0
    marginal_gain: float | None = None  # at the moment of selection
    redundancy: float | None = None  # standalone_gain - marginal_gain
    ratio: float | None = None  # marginal_gain / estimated tokens (optimized)


@dataclass
class Selection:
    order: list[Item]  # selected items in output order
    document: str
    used_tokens: int
    utility: float
    decisions: dict[str, Decision] = field(default_factory=dict)
    note: str = ""


class Objective:
    """The utility ``U`` over a fixed candidate set."""

    def __init__(
        self,
        items: list[Item],
        term_weights: dict[str, float],
        symbol_weights: dict[str, float],
        weights: Weights,
    ) -> None:
        self.weights = weights
        term_total = sum(term_weights.values())
        self.w = {t: v / term_total for t, v in term_weights.items()} if term_total else {}
        relevant = {s for item in items for s in item.symbols if s in symbol_weights}
        sym_total = sum(symbol_weights[s] for s in relevant)
        self.g = {s: symbol_weights[s] / sym_total for s in relevant} if sym_total else {}

    def prior(self, item: Item) -> float:
        return float(item.rank) ** -self.weights.rank_decay

    def gain(self, item: Item, terms: set[str], symbols: set[str]) -> float:
        a, b, c = self.weights.alpha, self.weights.beta, self.weights.gamma
        new_terms = sum(self.w.get(t, 0.0) for t in item.terms - terms)
        new_symbols = sum(self.g.get(s, 0.0) for s in item.symbols - symbols)
        return a * self.prior(item) + b * new_terms + c * new_symbols

    def value(self, chosen: list[Item]) -> float:
        terms: set[str] = set()
        symbols: set[str] = set()
        total = 0.0
        for item in chosen:
            total += self.gain(item, terms, symbols)
            terms |= item.terms
            symbols |= item.symbols
        return total


RenderFn = Callable[[Item, int], str]  # (item, 1-based position) -> Markdown piece
CountFn = Callable[[str], int]


def select(
    strategy: str,
    items: list[Item],
    objective: Objective,
    header: str,
    budget: int,
    render: RenderFn,
    count: CountFn,
) -> Selection:
    """Choose items whose rendered document (``header`` + pieces) fits ``budget``.

    ``items`` must already exclude stale and duplicate candidates. Every
    accepted item is checked by counting the complete document, so the
    returned ``used_tokens`` is exact and never exceeds ``budget``.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; choose one of: {', '.join(STRATEGIES)}")
    header_tokens = count(header)
    if header_tokens > budget:
        raise ValueError("header exceeds budget")  # callers check this first
    decisions = {
        i.key: Decision(i.key, i.rank, False, "", standalone_gain=objective.gain(i, set(), set()))
        for i in items
    }
    if strategy == "greedy":
        chosen = _greedy(items, objective, header, budget, render, count, decisions)
        note = "rank order; each fragment kept if the whole document still fits"
    else:
        chosen = _cost_benefit(items, objective, header, budget, render, count, decisions)
        note = "cost-benefit greedy on U, compared with the best single fragment"
        single = _best_single(items, objective, header, budget, render, count)
        if single is not None and objective.value([single]) > objective.value(chosen):
            for key in [c.key for c in chosen]:
                decisions[key].selected = False
                decisions[key].reason = "replaced: a single fragment had higher utility"
            decisions[single.key].selected = True
            decisions[single.key].reason = "best single fragment (higher U than greedy set)"
            decisions[single.key].marginal_gain = decisions[single.key].standalone_gain
            decisions[single.key].redundancy = 0.0
            chosen = [single]
            note += "; the best single fragment won"
    document, used, order = _finalize(chosen, header, budget, render, count, decisions)
    return Selection(order, document, used, objective.value(order), decisions, note)


def _accept(
    item: Item, decision: Decision, gain: float, terms: set[str], symbols: set[str]
) -> None:
    decision.selected = True
    decision.marginal_gain = gain
    decision.redundancy = max(0.0, decision.standalone_gain - gain)
    terms |= item.terms
    symbols |= item.symbols


def _greedy(
    items: list[Item],
    objective: Objective,
    header: str,
    budget: int,
    render: RenderFn,
    count: CountFn,
    decisions: dict[str, Decision],
) -> list[Item]:
    chosen: list[Item] = []
    document, used = header, count(header)
    terms: set[str] = set()
    symbols: set[str] = set()
    for item in sorted(items, key=lambda i: (i.rank, i.key)):
        piece = render(item, len(chosen) + 1)
        fits, new_total = _fits(document, piece, used, budget, count)
        if not fits:
            decisions[item.key].reason = _no_room(new_total - used, budget - used)
            continue
        gain = objective.gain(item, terms, symbols)
        _accept(item, decisions[item.key], gain, terms, symbols)
        decisions[item.key].reason = "next in rank order and fits"
        chosen.append(item)
        document += piece
        used = new_total
    return chosen


def _cost_benefit(
    items: list[Item],
    objective: Objective,
    header: str,
    budget: int,
    render: RenderFn,
    count: CountFn,
    decisions: dict[str, Decision],
) -> list[Item]:
    estimate = {i.key: max(1, count(render(i, 1))) for i in items}
    remaining = sorted(items, key=lambda i: (i.rank, i.key))
    chosen: list[Item] = []
    document, used = header, count(header)
    terms: set[str] = set()
    symbols: set[str] = set()
    while remaining:
        scored = [
            (
                objective.gain(i, terms, symbols) / estimate[i.key],
                objective.gain(i, terms, symbols),
                i,
            )
            for i in remaining
        ]
        # Highest ratio; ties by higher gain, then better rank, then key.
        ratio, gain, best = max(scored, key=lambda s: (s[0], s[1], -s[2].rank, s[2].key))
        remaining.remove(best)
        decision = decisions[best.key]
        decision.ratio = ratio
        piece = render(best, len(chosen) + 1)
        fits, new_total = _fits(document, piece, used, budget, count)
        if not fits:
            decision.reason = _no_room(new_total - used, budget - used)
            continue
        _accept(best, decision, gain, terms, symbols)
        decision.reason = "highest marginal utility per token that fits"
        chosen.append(best)
        document += piece
        used = new_total
    return chosen


def _best_single(
    items: list[Item],
    objective: Objective,
    header: str,
    budget: int,
    render: RenderFn,
    count: CountFn,
) -> Item | None:
    best: Item | None = None
    best_value = -1.0
    for item in sorted(items, key=lambda i: (i.rank, i.key)):
        value = objective.gain(item, set(), set())
        if value > best_value and count(header + render(item, 1)) <= budget:
            best, best_value = item, value
    return best


def _fits(document: str, piece: str, used: int, budget: int, count: CountFn) -> tuple[bool, int]:
    standalone = count(piece)
    if standalone > budget - used + BOUNDARY_SLACK_TOKENS:
        return False, used + standalone
    new_total = count(document + piece)
    return new_total <= budget, new_total


def _no_room(needed: int, remaining: int) -> str:
    return f"needs ~{needed} tokens; only {remaining} remain in budget"


def _finalize(
    chosen: list[Item],
    header: str,
    budget: int,
    render: RenderFn,
    count: CountFn,
    decisions: dict[str, Decision],
) -> tuple[str, int, list[Item]]:
    """Render in rank order when that still fits, else keep the verified selection order."""
    for order in (sorted(chosen, key=lambda i: (i.rank, i.key)), chosen):
        document = header
        used = count(header)
        deltas: list[int] = []
        for position, item in enumerate(order, start=1):
            document += render(item, position)
            new_total = count(document)
            deltas.append(new_total - used)
            used = new_total
        if used <= budget:
            for item, delta in zip(order, deltas, strict=True):
                decisions[item.key].tokens = delta
            return document, used, order
    raise AssertionError("internal error: verified selection no longer fits")  # pragma: no cover
