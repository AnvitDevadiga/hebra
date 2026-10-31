from __future__ import annotations

import random

import pytest

from hebra.context.optimizer import Item, Objective, Weights, select
from hebra.context.tokenizer import get_token_counter

COUNT = get_token_counter().count
HEADER = "# Header\n\nSome fixed header text.\n"


def item(key: str, rank: int, terms: str = "", symbols: str = "") -> Item:
    return Item(key, rank, frozenset(terms.split()), frozenset(symbols.split()))


def renderer(sizes: dict[str, int]):  # type: ignore[no-untyped-def]
    """Pieces whose token size is controlled by repeating a word."""

    def render(it: Item, position: int) -> str:
        return f"\n## {position}. {it.key}\n\n" + "word " * sizes[it.key] + "\n"

    return render


TERM_W = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0}


def test_utility_is_monotone_and_submodular() -> None:
    rng = random.Random(7)
    pool = [
        item(f"i{n}", n + 1, " ".join(rng.sample("abcd", rng.randint(0, 3))), f"s{n % 3}")
        for n in range(12)
    ]
    obj = Objective(pool, TERM_W, {"s0": 2.0, "s1": 1.0, "s2": 0.5}, Weights(1, 1, 1))
    for _ in range(300):
        b = rng.sample(pool, rng.randint(0, 8))
        a = b[: rng.randint(0, len(b))]
        x = rng.choice([p for p in pool if p not in b] or pool)
        gain_a = obj.value([*a, x]) - obj.value(a)
        gain_b = obj.value([*b, x]) - obj.value(b)
        assert gain_a >= -1e-12  # monotone
        assert gain_a >= gain_b - 1e-12  # diminishing returns


@pytest.mark.parametrize("strategy", ["greedy", "optimized"])
def test_budget_is_never_exceeded_and_accounting_is_exact(strategy: str) -> None:
    rng = random.Random(11)
    items = [item(f"k{n}", n + 1, rng.choice(["a", "b", "a b", "c d"])) for n in range(15)]
    sizes = {it.key: rng.randint(5, 120) for it in items}
    obj = Objective(items, TERM_W, {}, Weights())
    for budget in [COUNT(HEADER), *range(20, 1500, 37)]:
        if COUNT(HEADER) > budget:
            continue
        sel = select(strategy, items, obj, HEADER, budget, renderer(sizes), COUNT)
        assert COUNT(sel.document) == sel.used_tokens <= budget
        assert sum(sel.decisions[i.key].tokens or 0 for i in sel.order) == (
            sel.used_tokens - COUNT(HEADER)
        )
        assert len({i.key for i in sel.order}) == len(sel.order)


def test_header_only_budget_selects_nothing() -> None:
    items = [item("a", 1, "a")]
    obj = Objective(items, TERM_W, {}, Weights())
    sel = select("optimized", items, obj, HEADER, COUNT(HEADER), renderer({"a": 10}), COUNT)
    assert sel.order == [] and sel.used_tokens == COUNT(HEADER)
    assert "needs" in sel.decisions["a"].reason


def test_optimized_prefers_new_information_over_redundancy() -> None:
    # A and B carry the same term; C carries a different one. Room for two.
    items = [item("A", 1, "a"), item("B", 2, "a"), item("C", 3, "b")]
    sizes = {"A": 40, "B": 40, "C": 40}
    obj = Objective(items, {"a": 1.0, "b": 1.0}, {}, Weights(alpha=0.2, beta=1.0))
    budget = COUNT(HEADER) + 2 * 55
    greedy = select("greedy", items, obj, HEADER, budget, renderer(sizes), COUNT)
    optimized = select("optimized", items, obj, HEADER, budget, renderer(sizes), COUNT)
    assert [i.key for i in greedy.order] == ["A", "B"]
    assert [i.key for i in optimized.order] == ["A", "C"]
    assert optimized.utility > greedy.utility
    assert greedy.decisions["B"].redundancy == pytest.approx(0.5)  # term a already covered


def test_best_single_fragment_can_win() -> None:
    # One large fragment covers everything; small ones are cheaper per unit
    # but only cover one term each, and taking them first leaves no room.
    items = [item("big", 1, "a b c d"), item("s1", 2, "a"), item("s2", 3, "a")]
    sizes = {"big": 200, "s1": 5, "s2": 5}
    obj = Objective(items, TERM_W, {}, Weights(alpha=0.0, beta=1.0))
    budget = COUNT(HEADER + renderer(sizes)(items[0], 1))
    sel = select("optimized", items, obj, HEADER, budget, renderer(sizes), COUNT)
    assert [i.key for i in sel.order] == ["big"]
    assert "best single fragment" in sel.note
    assert sel.used_tokens <= budget


def test_selection_is_deterministic() -> None:
    items = [item(f"k{n}", n + 1, "a b"[n % 3 :]) for n in range(10)]
    sizes = {it.key: 30 for it in items}
    obj = Objective(items, TERM_W, {}, Weights())
    first = select("optimized", items, obj, HEADER, 400, renderer(sizes), COUNT)
    for _ in range(3):
        again = select("optimized", items, obj, HEADER, 400, renderer(sizes), COUNT)
        assert again.document == first.document
        assert [i.key for i in again.order] == [i.key for i in first.order]


def test_validation() -> None:
    with pytest.raises(ValueError):
        Weights(alpha=-1)
    items = [item("a", 1, "a")]
    obj = Objective(items, TERM_W, {}, Weights())
    with pytest.raises(ValueError, match="unknown strategy"):
        select("magic", items, obj, HEADER, 500, renderer({"a": 1}), COUNT)


def test_symbol_weights_only_count_candidate_symbols() -> None:
    items = [item("A", 1, symbols="s1"), item("B", 2, symbols="s2")]
    obj = Objective(items, {}, {"s1": 1.0, "s2": 1.0, "unrelated": 100.0}, Weights(0, 0, 1))
    assert obj.value(items) == pytest.approx(1.0)  # normalised over reachable symbols
