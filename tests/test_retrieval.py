from __future__ import annotations

from pathlib import Path

import pytest

from conftest import fs_index
from hebra.errors import QueryError
from hebra.retrieval.search import GRAPH_MAX_NODES, GRAPH_SEED_HITS, RRF_K, search

REPO = {
    "shop/checkout.py": (
        "from shop.levy import _apply_levy\n"
        "\n"
        "def checkout_total(cart):\n"
        '    """Compute the checkout total for a cart."""\n'
        "    return _apply_levy(sum(cart))\n"
        "\n"
        "def dynamic_checkout(cart, name):\n"
        "    return getattr(__import__('shop.hidden'), name)(cart)\n"
    ),
    "shop/levy.py": "def _apply_levy(amount):\n    return amount * 1.07\n",
    "shop/hidden.py": "def secret_route(x):\n    return x\n",
    "shop/unrelated.py": "def parse_header(line):\n    return line.split(':')\n",
}
TASK = "checkout total for a cart"


@pytest.fixture
def repo(make_repo) -> Path:
    root = make_repo(REPO)
    fs_index(root)
    return root


def test_graph_mode_finds_callee_with_no_lexical_overlap(repo: Path) -> None:
    bm25 = search(repo, TASK, limit=10)
    assert "shop/levy.py" not in {h.path for h in bm25.hits}
    graph = search(repo, TASK, limit=10, mode="bm25_graph")
    levy = [h for h in graph.hits if h.path == "shop/levy.py"]
    assert levy and levy[0].bm25_rank is None and levy[0].graph_rank is not None
    assert levy[0].sources == ("graph",)
    assert levy[0].evidence == (
        "shop/checkout.py::checkout_total -> calls shop/levy.py::_apply_levy",
    )
    assert levy[0].symbols == ("shop/levy.py::_apply_levy",)


def test_dynamic_calls_are_not_followed(repo: Path) -> None:
    graph = search(repo, "dynamic checkout cart name", limit=20, mode="bm25_graph")
    assert "shop/hidden.py" not in {h.path for h in graph.hits}


def test_rrf_scores_follow_the_formula(repo: Path) -> None:
    for hit in search(repo, TASK, limit=10, mode="bm25_graph").hits:
        expected = sum(1 / (RRF_K + r) for r in (hit.bm25_rank, hit.graph_rank) if r)
        assert hit.score == pytest.approx(expected, abs=1e-9)


def test_graph_mode_keeps_top_bm25_order(make_repo) -> None:
    files: dict[str, str | bytes] = {
        f"m{i:02d}.py": f"def handler_{i}():\n    return helper_{i}()\n"
        f"def helper_{i}():\n    return {i}\n" + ("# request handler\n" * (i % 4))
        for i in range(25)
    }
    root = make_repo(files)
    fs_index(root)
    bm25 = search(root, "request handler", limit=GRAPH_SEED_HITS)
    graph = search(root, "request handler", limit=40, mode="bm25_graph")
    top = [(h.path, h.start_line) for h in bm25.hits]
    assert [(h.path, h.start_line) for h in graph.hits[:GRAPH_SEED_HITS]] == top
    assert sum(1 for h in graph.hits if h.sources == ("graph",)) <= GRAPH_MAX_NODES


def test_retrieval_is_deterministic(repo: Path) -> None:
    for mode in ("bm25", "bm25_graph"):
        first = search(repo, TASK, limit=10, mode=mode)
        for _ in range(3):
            assert search(repo, TASK, limit=10, mode=mode).hits == first.hits


def test_freshness_applies_to_graph_candidates(repo: Path) -> None:
    (repo / "shop/levy.py").write_text("def _apply_levy(amount):\n    return amount * 2\n")
    graph = search(repo, TASK, limit=10, mode="bm25_graph")
    levy = [h for h in graph.hits if h.path == "shop/levy.py"]
    assert levy and levy[0].source_status == "modified"


def test_term_weights_are_positive_idf(repo: Path) -> None:
    response = search(repo, TASK, limit=5)
    assert set(response.term_weights) == set(response.terms)
    assert all(w > 0 for w in response.term_weights.values())
    # "return" occurs in every chunk, "header" in one: the rarer term weighs more
    weights = search(repo, "return parse header", limit=5).term_weights
    assert weights["header"] > weights["return"] > 0


def test_invalid_mode(repo: Path) -> None:
    with pytest.raises(QueryError, match="unknown retrieval mode"):
        search(repo, TASK, mode="semantic")
