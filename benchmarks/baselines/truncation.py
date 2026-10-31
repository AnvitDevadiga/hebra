"""Simple truncation baseline.

Render the BM25-ranked fragments in rank order with Hebra's own header and
fragment format, with no budget awareness, then cut the token sequence at
the budget. A fragment counts as included only through the content lines
that survive the cut completely, so a label is "hit" only if one of its lines
is fully present.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hebra.context.render import render_fragment, render_header, selection_reason
from hebra.context.tokenizer import get_token_counter
from hebra.paths import resolve_repository
from hebra.retrieval.search import SearchHit, search


@dataclass(frozen=True)
class IncludedFragment:
    path: str
    start_line: int
    end_line: int  # last fully included line
    content: str  # fully included lines only
    terms: frozenset[str]
    kind: str


def truncate_pack(
    repo: Path, task: str, budget: int, *, limit: int = 50, encoding: str = "o200k_base"
) -> tuple[list[IncludedFragment], int]:
    """Fragments surviving a hard token cut, and the token count of the cut text."""
    import tiktoken

    root = resolve_repository(repo)
    response = search(root, task, limit=limit)
    hits = [h for h in response.hits if h.source_status == "current"]
    header = render_header(
        task,
        root.name,
        response.index_revision,
        budget,
        encoding,
        "not checked (truncation baseline)",
        "bm25 retrieval, truncation",
    )
    document = header
    spans: list[tuple[int, SearchHit]] = []  # (offset of content start, hit)
    for position, hit in enumerate(hits, start=1):
        piece = render_fragment(position, hit, selection_reason(hit, len(hits)))
        content_offset = len(document) + piece.index("\n" + hit.content + "\n") + 1
        spans.append((content_offset, hit))
        document += piece
    encoder = tiktoken.get_encoding(encoding)
    tokens = encoder.encode_ordinary(document)[:budget]
    prefix = encoder.decode(tokens)
    included: list[IncludedFragment] = []
    for offset, hit in spans:
        available = len(prefix) - offset
        if available <= 0:
            break
        lines = hit.content.split("\n")
        kept: list[str] = []
        used = 0
        for line in lines:
            if used + len(line) > available:
                break
            kept.append(line)
            used += len(line) + 1
        if kept:
            included.append(
                IncludedFragment(
                    hit.path,
                    hit.start_line,
                    hit.start_line + len(kept) - 1,
                    "\n".join(kept),
                    frozenset(hit.matched_terms),
                    hit.kind,
                )
            )
    return included, get_token_counter(encoding).count(prefix)
