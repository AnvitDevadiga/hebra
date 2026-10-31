from __future__ import annotations

from pathlib import Path

import pytest

from conftest import SAMPLE_PY, fs_index, git, requires_git
from hebra.errors import IndexNotFoundError, QueryError
from hebra.retrieval.search import build_match_expression, search
from hebra.terms import query_terms


@pytest.fixture
def indexed_repo(make_repo) -> Path:
    root = make_repo(
        {
            "pkg/config.py": SAMPLE_PY,
            "pkg/http.py": "def send_request(url):\n    return url\n",
            "docs/guide.md": "# Guide\n\nHow to configure the loader.\n",
        }
    )
    fs_index(root)
    return root


def test_missing_index(tmp_path: Path) -> None:
    with pytest.raises(IndexNotFoundError, match="hebra index"):
        search(tmp_path, "anything")


@pytest.mark.parametrize("task", ["", "   ", "\n\t"])
def test_empty_task(indexed_repo: Path, task: str) -> None:
    with pytest.raises(QueryError, match="empty"):
        search(indexed_repo, task)


@pytest.mark.parametrize("task", ["the and of", "!!! ??? ---", "a b c", "AND OR NOT"])
def test_task_without_terms(indexed_repo: Path, task: str) -> None:
    with pytest.raises(QueryError, match="no searchable terms"):
        search(indexed_repo, task)


@pytest.mark.parametrize(
    "task",
    [
        'config" OR 1=1 --',
        "config'; DROP TABLE chunks; --",
        "NEAR(config loader)",
        "config*",
        "path:config",
        "^config + -loader",
        '"""unbalanced',
        "config AND OR NOT loader",
        "config\x00loader",
    ],
)
def test_malformed_queries_are_treated_as_text(indexed_repo: Path, task: str) -> None:
    response = search(indexed_repo, task)
    for hit in response.hits:
        assert hit.path
    # The index must be intact afterwards.
    assert search(indexed_repo, "config loader").hits


def test_match_expression_quotes_every_term() -> None:
    assert build_match_expression(["config", "and"]) == '"config" OR "and"'
    with pytest.raises(QueryError):
        build_match_expression(['bad"term'])


def test_query_terms_split_identifiers() -> None:
    assert query_terms("Fix ConfigLoader.load_file in HTTPServer") == [
        "fix",
        "configloader",
        "config",
        "loader",
        "load",
        "file",
        "httpserver",
        "http",
        "server",
    ]


def test_camel_case_task_matches_identifier(indexed_repo: Path) -> None:
    response = search(indexed_repo, "config loader")
    top = response.hits[0]
    assert top.path == "pkg/config.py"
    assert top.symbol == "ConfigLoader"
    assert {"config", "loader"} <= set(top.matched_terms)


def test_results_carry_location_and_score(indexed_repo: Path) -> None:
    response = search(indexed_repo, "send request url")
    top = response.hits[0]
    assert (top.path, top.start_line, top.end_line) == ("pkg/http.py", 1, 2)
    assert top.kind == "function" and top.symbol == "send_request"
    assert top.score > 0
    assert [h.rank for h in response.hits] == list(range(1, len(response.hits) + 1))
    assert all(a.score >= b.score for a, b in zip(response.hits, response.hits[1:], strict=False))


def test_stable_ordering_for_tied_scores(make_repo) -> None:
    same = "def duplicate_helper():\n    return 42\n"
    root = make_repo({"b/util.py": same, "a/util.py": same, "c/util.py": same})
    fs_index(root)
    first = search(root, "duplicate helper")
    assert [h.path for h in first.hits] == ["a/util.py", "b/util.py", "c/util.py"]
    for _ in range(3):
        assert search(root, "duplicate helper").hits == first.hits


def test_unicode_query(make_repo) -> None:
    root = make_repo({"i18n.py": "GREETING = 'こんにちは世界'\nCAFÉ_NAME = 'café'\n"})
    fs_index(root)
    assert search(root, "café").hits
    assert search(root, "こんにちは世界").hits


def test_no_matches_returns_empty(indexed_repo: Path) -> None:
    assert search(indexed_repo, "kubernetes helm chart").hits == []


def test_limit_validation(indexed_repo: Path) -> None:
    assert len(search(indexed_repo, "config", limit=1).hits) == 1
    for bad in (0, -1, 10_000):
        with pytest.raises(QueryError):
            search(indexed_repo, "config", limit=bad)


@requires_git
def test_stale_index_warning(make_git_repo) -> None:
    from hebra.indexing import index_repository

    root = make_git_repo({"a.py": "def alpha():\n    pass\n"})
    index_repository(root)
    assert search(root, "alpha").warnings == []
    (root / "b.py").write_text("x = 1\n")
    git(root, "add", "b.py")
    git(root, "commit", "-q", "-m", "second")
    assert "run `hebra index`" in search(root, "alpha").warnings[0]
