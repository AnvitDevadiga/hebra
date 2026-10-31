from __future__ import annotations

import json
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from conftest import SAMPLE_PY, fs_index
from hebra.context.packer import pack_context
from hebra.context.tokenizer import get_token_counter
from hebra.errors import BudgetError, QueryError, TokenizerError

TASK = "Fix the config loader so load_file handles missing files"


@pytest.fixture
def repo(make_repo) -> Path:
    files: dict[str, str | bytes] = {"pkg/config.py": SAMPLE_PY}
    for i in range(8):
        body = "".join(f"    config_value_{j} = load_file('f{j}')\n" for j in range(i * 3 + 2))
        files[f"pkg/mod{i}.py"] = f"def loader_step_{i}(config):\n{body}"
    files["docs/config.md"] = "# Config\n\nThe loader reads config files.\n"
    root = make_repo(files)
    fs_index(root)
    return root


@pytest.mark.parametrize("budget", [0, -1, -8000])
def test_non_positive_budget_rejected(repo: Path, budget: int) -> None:
    with pytest.raises(BudgetError, match="positive"):
        pack_context(repo, TASK, budget)


def test_non_integer_budget_rejected(repo: Path) -> None:
    with pytest.raises(BudgetError):
        pack_context(repo, TASK, True)  # type: ignore[arg-type]


def test_budget_smaller_than_header(repo: Path) -> None:
    with pytest.raises(BudgetError, match="header alone needs"):
        pack_context(repo, TASK, 10)


def test_unsupported_encoding(repo: Path) -> None:
    with pytest.raises(TokenizerError, match="unsupported encoding"):
        pack_context(repo, TASK, 1000, encoding="gpt2-made-up")


def test_empty_task(repo: Path) -> None:
    with pytest.raises(QueryError):
        pack_context(repo, "   ", 1000)


@pytest.mark.parametrize("encoding", ["o200k_base", "cl100k_base"])
@pytest.mark.parametrize("budget", [120, 150, 200, 300, 450, 700, 1000, 2000, 50_000])
def test_output_never_exceeds_budget_and_accounting_is_exact(
    repo: Path, budget: int, encoding: str
) -> None:
    result = pack_context(repo, TASK, budget, encoding=encoding)
    counter = get_token_counter(encoding)
    actual = counter.count(result.markdown)
    assert actual == result.used_tokens <= budget
    assert result.header_tokens + sum(f.tokens for f in result.selected) == result.used_tokens
    report = result.to_dict()
    assert report["budget"]["used"] == actual
    assert report["budget"]["remaining"] == budget - actual


def test_tight_budget_selects_fewer_and_explains_exclusions(repo: Path) -> None:
    roomy = pack_context(repo, TASK, 50_000)
    tight = pack_context(repo, TASK, 400)
    assert len(roomy.selected) > len(tight.selected) >= 1
    assert roomy.excluded == []
    assert tight.excluded
    assert all("remain in budget" in e.reason for e in tight.excluded)
    # Every candidate is accounted for exactly once.
    assert len(tight.selected) + len(tight.excluded) == tight.candidates


def test_smallest_feasible_budget_holds_only_the_header(repo: Path) -> None:
    # The header mentions the budget, so its size depends on the budget's digits;
    # search for the smallest budget that does not raise.
    for budget in range(1, 500):
        try:
            result = pack_context(repo, TASK, budget)
        except BudgetError:
            continue
        break
    else:
        pytest.fail("no feasible budget found")
    assert result.selected == []
    assert result.used_tokens == result.header_tokens == budget
    assert any("none fit" in w for w in result.warnings)


def test_selected_fragments_are_in_rank_order_with_reasons(repo: Path) -> None:
    result = pack_context(repo, TASK, 50_000)
    ranks = [f.hit.rank for f in result.selected]
    assert ranks == sorted(ranks)
    for fragment in result.selected:
        assert fragment.reason.startswith(f"BM25 rank {fragment.hit.rank} of")
        heading = f"`{fragment.hit.path}` lines {fragment.hit.start_line}-{fragment.hit.end_line}"
        assert heading in result.markdown


def test_duplicate_content_is_excluded(make_repo) -> None:
    same = "def shared_config_loader():\n    return 'config'\n"
    root = make_repo({"a/x.py": same, "b/x.py": same})
    fs_index(root)
    result = pack_context(root, "shared config loader", 5000)
    assert [f.hit.path for f in result.selected] == ["a/x.py"]
    assert result.excluded[0].hit.path == "b/x.py"
    assert "identical content" in result.excluded[0].reason


def test_no_matches(repo: Path) -> None:
    result = pack_context(repo, "kubernetes helm", 1000)
    assert result.selected == [] and result.candidates == 0
    assert any("no indexed fragments" in w for w in result.warnings)


def test_special_token_text_is_counted_as_plain_text(make_repo) -> None:
    root = make_repo({"tok.py": "MARKER = '<|endoftext|>'  # special token text\n"})
    fs_index(root)
    result = pack_context(root, "special token marker", 2000)
    assert "<|endoftext|>" in result.markdown
    assert get_token_counter().count(result.markdown) == result.used_tokens


def _code_blocks(markdown: str) -> list[tuple[str, str]]:
    tokens = MarkdownIt("commonmark").parse(markdown)
    return [(t.info, t.content) for t in tokens if t.type == "fence"]


def test_markdown_is_valid_and_preserves_content(repo: Path) -> None:
    result = pack_context(repo, TASK, 50_000)
    blocks = _code_blocks(result.markdown)
    assert len(blocks) == len(result.selected)
    for (info, content), fragment in zip(blocks, result.selected, strict=True):
        assert content == fragment.hit.content + "\n"
        assert info == ("" if fragment.hit.language == "text" else fragment.hit.language)


def test_markdown_fences_survive_backticks_in_content(make_repo) -> None:
    doc = 'def render_markdown():\n    return """\n```python\nprint(1)\n```\n````\n"""\n'
    root = make_repo({"md.py": doc})
    fs_index(root)
    result = pack_context(root, "render markdown", 2000)
    blocks = _code_blocks(result.markdown)
    assert len(blocks) == 1
    assert blocks[0][1] == doc


def test_json_report_is_valid_and_complete(repo: Path) -> None:
    result = pack_context(repo, TASK, 600)
    data = json.loads(json.dumps(result.to_dict()))
    assert data["schema"] == "hebra.pack/v1"
    assert data["tokenizer"] == {
        "library": "tiktoken",
        "encoding": "o200k_base",
        "note": data["tokenizer"]["note"],
    }
    assert data["context"] == result.markdown
    assert data["repository"]["index_revision"] is None
    for item in data["selected"]:
        assert {"path", "start_line", "end_line", "tokens", "reason", "score"} <= item.keys()
    for item in data["excluded"]:
        assert {"path", "start_line", "end_line", "reason"} <= item.keys()


def test_packing_is_reproducible(repo: Path) -> None:
    first = pack_context(repo, TASK, 900).to_dict()
    second = pack_context(repo, TASK, 900).to_dict()
    assert json.dumps(first) == json.dumps(second)
