"""Baseline token-budget-aware context packing.

Policy (Phase 1, deliberately simple):

1. Retrieve the top-N BM25 candidates for the task.
2. Walk them in rank order. Skip exact duplicate content. Append a fragment
   only if the *whole rendered document* still fits the budget, re-counted
   with the real tokenizer after each addition. Fragments are never truncated.
3. Verify the final document against the budget before returning it.

Because acceptance is decided on the full rendered document, the budget
guarantee includes all formatting overhead and any token merges across
fragment boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hebra import __version__
from hebra.context.render import render_fragment, render_header, selection_reason
from hebra.context.tokenizer import DEFAULT_ENCODING, SUPPORTED_ENCODINGS, get_token_counter
from hebra.errors import BudgetError
from hebra.retrieval.search import SearchHit, search

DEFAULT_CANDIDATES = 50
MAX_BUDGET = 2_000_000
# Pre-filter slack: a fragment whose stand-alone token count exceeds the
# remaining budget by more than this is skipped without a full re-count.
# This only avoids work; acceptance always uses the exact full count.
_BOUNDARY_SLACK_TOKENS = 16

PACK_SCHEMA = "hebra.pack/v1"


@dataclass(frozen=True)
class Fragment:
    hit: SearchHit
    tokens: int  # exact marginal tokens this fragment added to the document
    reason: str


@dataclass(frozen=True)
class Exclusion:
    hit: SearchHit
    reason: str


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
    terms: list[str]
    selected: list[Fragment]
    excluded: list[Exclusion]
    markdown: str
    warnings: list[str] = field(default_factory=list)

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
            "budget": {
                "limit": self.budget,
                "used": self.used_tokens,
                "remaining": self.budget - self.used_tokens,
                "header_tokens": self.header_tokens,
                "fragment_tokens": self.used_tokens - self.header_tokens,
            },
            "candidates_considered": self.candidates,
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


def pack_context(
    repo_root: Path,
    task: str,
    budget: int,
    *,
    encoding: str = DEFAULT_ENCODING,
    candidates: int = DEFAULT_CANDIDATES,
    db_path: Path | None = None,
) -> PackResult:
    """Select ranked fragments for ``task`` whose rendered Markdown fits ``budget``."""
    validate_budget(budget)
    counter = get_token_counter(encoding)
    response = search(repo_root, task, limit=candidates, db_path=db_path)

    document = render_header(task, repo_root.name, response.index_revision, budget, encoding)
    header_tokens = counter.count(document)
    if header_tokens > budget:
        raise BudgetError(
            f"budget of {budget} tokens is too small: the context header alone needs "
            f"{header_tokens} tokens ({encoding})"
        )

    used = header_tokens
    selected: list[Fragment] = []
    excluded: list[Exclusion] = []
    seen_content: dict[str, SearchHit] = {}
    total = len(response.hits)

    for hit in response.hits:
        duplicate_of = seen_content.get(hit.content_sha256)
        if duplicate_of is not None:
            excluded.append(
                Exclusion(
                    hit,
                    f"identical content already selected from {duplicate_of.path}:"
                    f"{duplicate_of.start_line}-{duplicate_of.end_line}",
                )
            )
            continue

        reason = selection_reason(hit, total)
        piece = render_fragment(len(selected) + 1, hit, reason)
        remaining = budget - used
        standalone = counter.count(piece)
        if standalone > remaining + _BOUNDARY_SLACK_TOKENS:
            excluded.append(
                Exclusion(hit, f"needs ~{standalone} tokens; only {remaining} remain in budget")
            )
            continue
        new_total = counter.count(document + piece)
        if new_total > budget:
            excluded.append(
                Exclusion(
                    hit,
                    f"needs {new_total - used} tokens; only {remaining} remain in budget",
                )
            )
            continue

        selected.append(Fragment(hit, new_total - used, reason))
        seen_content[hit.content_sha256] = hit
        document += piece
        used = new_total

    final_tokens = counter.count(document)
    if final_tokens != used or final_tokens > budget:  # pragma: no cover - invariant
        raise AssertionError(
            f"internal error: packed context has {final_tokens} tokens, accounted {used}, "
            f"budget {budget}"
        )

    warnings = list(response.warnings)
    if not response.hits:
        warnings.append("no indexed fragments matched the task terms")
    elif not selected:
        warnings.append("matching fragments were found but none fit within the budget")

    return PackResult(
        task=task,
        repo_root=str(repo_root),
        index_path=response.index_path,
        index_revision=response.index_revision,
        current_revision=response.current_revision,
        encoding=encoding,
        budget=budget,
        header_tokens=header_tokens,
        used_tokens=used,
        candidates=total,
        terms=response.terms,
        selected=selected,
        excluded=excluded,
        markdown=document,
        warnings=warnings,
    )
