"""Command-line interface: ``hebra index | search | pack | stats``.

Data outputs (pack output, ``--json``) are written to stdout as UTF-8 bytes
regardless of the locale, so a limited terminal encoding can neither crash
the command nor alter the token-counted text. Human-readable lines use
``backslashreplace`` and escape untrusted paths.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hebra import __version__
from hebra.config import IndexConfig, ScanConfig
from hebra.context.optimizer import STRATEGIES
from hebra.context.packer import DEFAULT_CANDIDATES, MAX_BUDGET, PackResult, pack_context
from hebra.context.render import escape_untrusted
from hebra.context.tokenizer import DEFAULT_ENCODING, SUPPORTED_ENCODINGS
from hebra.errors import HebraError
from hebra.graph.traverse import MAX_HOPS, explore, find_symbols
from hebra.indexing import index_repository, index_summary, store
from hebra.memory import KINDS as MEMORY_KINDS
from hebra.memory import MemoryView, open_memory
from hebra.paths import index_location, resolve_repository, write_output_file
from hebra.retrieval.search import DEFAULT_LIMIT, MAX_LIMIT, MODES, search


def _bounded_int(low: int, high: int) -> Any:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from None
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}, got {number}")
        return number

    return parse


def _emit(text: str) -> None:
    """Write data output as UTF-8 bytes, independent of the stdout encoding."""
    sys.stdout.flush()
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:  # pragma: no cover - replaced stdout without a byte layer
        sys.stdout.write(text)
    else:
        buffer.write(text.encode("utf-8"))
        buffer.flush()


def _print_json(data: object) -> None:
    _emit(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _warn(messages: Sequence[str]) -> None:
    for message in messages:
        print(f"hebra: warning: {message}", file=sys.stderr)


def _db_path(args: argparse.Namespace) -> Path | None:
    return Path(args.db) if args.db else None


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in counts.items()) or "none"


def cmd_index(args: argparse.Namespace) -> int:
    repo = resolve_repository(Path(args.repository))
    scan = ScanConfig(
        max_file_bytes=args.max_file_bytes,
        max_files=args.max_files,
        use_git=not args.no_git,
        include_untracked=args.include_untracked,
    )
    report = index_repository(repo, _db_path(args), IndexConfig(scan=scan))
    if args.json:
        _print_json(report.to_dict())
    else:
        source = "git file list" if report.file_source == "git" else "filesystem walk"
        print(f"Indexed {report.repo_root} ({source})")
        print(f"  revision:  {report.head_revision or 'none'}")
        print(
            f"  files:     {report.files_indexed} indexed "
            f"({report.files_added} added, {report.files_changed} changed, "
            f"{report.files_unchanged} unchanged, {report.files_removed} removed)"
        )
        print(f"  chunks:    {report.chunks_written} written")
        print(f"  skipped:   {_format_counts(report.skipped)}")
        print(f"  index:     {report.index_path}")
    _warn(report.warnings)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    repo = resolve_repository(Path(args.repository))
    response = search(
        repo, args.task, limit=args.limit, db_path=_db_path(args), mode=args.retrieval
    )
    if args.json:
        _print_json(
            {
                "task": response.task,
                "query_terms": response.terms,
                "index_revision": response.index_revision,
                "retrieval": response.mode,
                "results": [h.to_dict(include_content=args.show_content) for h in response.hits],
                "warnings": response.warnings,
            }
        )
    else:
        print(f"Query terms: {', '.join(response.terms)}")
        print(f"Results: {len(response.hits)} (BM25, lexical)")
        for hit in response.hits:
            symbol = f" {escape_untrusted(hit.symbol)}" if hit.symbol else ""
            matched = ", ".join(hit.matched_terms)
            stale = "" if hit.source_status == "current" else f"  [STALE: {hit.source_status}]"
            if response.mode != "bm25":
                stale += f"  ({'+'.join(hit.sources)})"
            print(
                f"{hit.rank:>3}. {hit.score:>9.4f}  "
                f"{escape_untrusted(hit.path)}:{hit.start_line}-{hit.end_line}"
                f"  {hit.kind}{symbol}  [{matched}]{stale}"
            )
            if args.show_content:
                for line in hit.content.splitlines():
                    print(f"       | {escape_untrusted(line)}")
    _warn(response.warnings)
    return 0


def cmd_pack(args: argparse.Namespace) -> int:
    repo = resolve_repository(Path(args.repository))
    result = pack_context(
        repo,
        args.task,
        args.budget,
        encoding=args.encoding,
        candidates=args.candidates,
        db_path=_db_path(args),
        retrieval=args.retrieval,
        strategy=args.strategy,
        memory=_memory_views(repo, args) if args.memory else None,
    )
    if args.format == "json":
        output = json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n"
    else:
        output = result.markdown
    if args.output:
        write_output_file(Path(args.output), output.encode("utf-8"))
    else:
        _emit(output)
    memory_note = f", {len(result.memory)} memory record(s)" if args.memory else ""
    print(
        f"hebra: packed {len(result.selected)} fragment(s){memory_note}, "
        f"{result.used_tokens}/{result.budget} tokens ({result.encoding}); "
        f"{len(result.excluded)} candidate(s) excluded",
        file=sys.stderr,
    )
    if args.trace:
        _print_trace(result)
    _warn(result.warnings)
    return 0


def _memory_views(repo: Path, args: argparse.Namespace) -> list[MemoryView]:
    """Memory records for packing; a repository without a memory store has none."""
    db = Path(args.memory_db) if args.memory_db else None
    if db is None and not (repo / ".hebra" / "memory.sqlite3").exists():
        return []
    with open_memory(repo, db, create=False) as memory:
        return memory.views()


def _print_trace(result: PackResult) -> None:
    print(
        f"hebra: trace ({result.retrieval} retrieval, {result.strategy} selection; "
        f"U = {result.utility:.4f}; {result.selection_note})",
        file=sys.stderr,
    )
    for d in result.trace:
        mark = "+" if d.selected else "-"
        gain = "" if d.marginal_gain is None else f" gain={d.marginal_gain:.4f}"
        red = "" if not d.redundancy else f" redundancy={d.redundancy:.4f}"
        tokens = "" if d.tokens is None else f" tokens={d.tokens}"
        print(
            f"  {mark} #{d.rank:<3} {escape_untrusted(d.key)}{tokens}{gain}{red}  {d.reason}",
            file=sys.stderr,
        )


def cmd_memory(args: argparse.Namespace) -> int:
    repo = resolve_repository(Path(args.repository))
    db = Path(args.memory_db) if args.memory_db else None
    with open_memory(
        repo,
        db,
        create=args.memory_command == "add",
        writable=args.memory_command == "invalidate",
    ) as memory:
        if args.memory_command == "add":
            views = [memory.add(args.kind, args.text, args.ref or [])]
        elif args.memory_command == "invalidate":
            views = [memory.invalidate(args.id, args.reason)]
        elif args.memory_command == "show":
            views = [memory.view(memory.get(args.id))]
        else:
            views = memory.views(include_invalidated=args.all)
    if args.json:
        _print_json([v.to_dict() for v in views])
        return 0
    for view in views:
        r = view.record
        print(f"{r.id}  {r.kind:<16} {view.status:<13} {r.provenance}")
        print(f"    {escape_untrusted(r.content)}")
        for check in view.checks:
            ref = check.ref
            detail = f" ({check.detail})" if check.detail else ""
            print(
                f"    ref {escape_untrusted(ref.path)}:{ref.start_line}-{ref.end_line}  "
                f"{check.status}{escape_untrusted(detail)}"
            )
        if view.revision_note:
            print(f"    {view.revision_note}")
        if r.invalidated_reason:
            print(f"    invalidated: {escape_untrusted(r.invalidated_reason)}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    repo = resolve_repository(Path(args.repository))
    summary = index_summary(repo, _db_path(args))
    if args.json:
        _print_json(summary)
        return 0
    last_run: dict[str, Any] = summary["last_run"]
    print(f"Index:      {summary['index_path']} ({summary['index_size_bytes']} bytes)")
    print(f"Repository: {summary['repo_root']}")
    print(f"Revision:   {summary['head_revision'] or 'none'}")
    print(f"Indexed at: {summary['indexed_at']}")
    print(
        f"Files:      {summary['files']} ({summary['source_lines']} lines, "
        f"{summary['source_bytes']} bytes)"
    )
    print(f"Chunks:     {summary['chunks']}")
    print(f"Symbols:    {summary['symbols']}")
    print(f"Imports:    {summary['imports']}")
    print(f"Languages:  {_format_counts(summary['files_by_language'])}")
    print(f"Parsers:    {_format_counts(summary['files_by_parser'])}")
    print(f"Chunk kinds:  {_format_counts(summary['chunks_by_kind'])}")
    print(f"Symbol kinds: {_format_counts(summary['symbols_by_kind'])}")
    print(f"Graph edges:  {_format_counts(summary['edges_by_status'])}")
    print(f"Resolved:     {_format_counts(summary['resolved_edges_by_kind'])}")
    print(
        f"Last scan:  source={last_run.get('file_source')}, "
        f"skipped: {_format_counts(last_run.get('skipped') or {})}"
    )
    _warn(last_run.get("warnings") or [])
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    repo = resolve_repository(Path(args.repository))
    location = index_location(repo, _db_path(args))
    conn = store.open_for_read(location)
    try:
        with store.database_errors(location.path):
            matches = find_symbols(conn, args.symbol)
            if not matches:
                raise HebraError(f"no indexed symbol matches {args.symbol!r}")
            results = [explore(conn, m, max_hops=args.depth, max_nodes=args.limit) for m in matches]
    finally:
        conn.close()
    if args.json:
        _print_json({"query": args.symbol, "matches": [r.to_dict() for r in results]})
        return 0
    for r in results:
        sym = r.symbol
        print(
            f"{escape_untrusted(sym.symbol_id)}  ({sym.kind}, lines {sym.start_line}-"
            f"{sym.end_line})"
        )
        for kind, status, target, detail, line in r.outgoing:
            note = "" if status == "resolved" else f"  [{status}: {detail}]"
            print(f"  -> {kind:<10} {escape_untrusted(target)}  (line {line}){note}")
        for kind, src, line in r.incoming:
            print(f"  <- {kind:<10} {escape_untrusted(src)}  (line {line})")
        if r.neighborhood:
            print(f"  within {args.depth} hop(s): {len(r.neighborhood)} symbol(s)")
            for n in r.neighborhood:
                arrow = "->" if n.direction == "out" else "<-"
                print(f"     {n.hops} {arrow} {n.edge_kind:<10} {escape_untrusted(n.node)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hebra",
        description="Hebra: repository-aware context for AI coding agents. "
        "The right context. Nothing extra.",
    )
    parser.add_argument("--version", action="version", version=f"hebra {__version__}")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show full tracebacks for errors (also enabled by HEBRA_DEBUG=1)",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("repository", help="path to the repository root")
        p.add_argument(
            "--db",
            metavar="PATH",
            help="index database path (default: <repository>/.hebra/index.sqlite3)",
        )

    p_index = sub.add_parser(
        "index",
        help="build or incrementally update the repository index",
        description="Scan a repository and build or update its local SQLite index. Unchanged "
        "files are skipped by content hash; deleted and newly excluded files are removed. In "
        "Git repositories the paths are those in Git's index (`git ls-files --cached`): "
        "committed files plus newly staged ones, read with their current working-tree "
        "content, so unstaged edits are included; untracked files are not. If Git metadata "
        "exists but the listing fails, indexing stops. Files with credential-like names "
        "(.env, *.pem, id_rsa, ...) and files containing PEM private keys are always skipped; "
        "this is a filename/pattern filter, not a scanner for secrets inside other files. If "
        "files change during the scan it is retried (at most 3 attempts) or fails.",
    )
    add_common(p_index)
    p_index.add_argument(
        "--max-file-bytes",
        type=_bounded_int(1, 100_000_000),
        default=ScanConfig.max_file_bytes,
        help="skip files larger than this (default: %(default)s)",
    )
    p_index.add_argument(
        "--max-files",
        type=_bounded_int(1, 10_000_000),
        default=ScanConfig.max_files,
        help="stop accepting files after this many (default: %(default)s)",
    )
    p_index.add_argument(
        "--include-untracked",
        action="store_true",
        help="Git repositories: also index untracked files that .gitignore does not exclude",
    )
    p_index.add_argument(
        "--no-git",
        action="store_true",
        help="walk the filesystem instead of using git ls-files; .gitignore is NOT applied",
    )
    p_index.add_argument("--json", action="store_true", help="print the report as JSON")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser(
        "search",
        help="rank indexed code against a task (BM25)",
        description="Lexical BM25 search over indexed chunks using SQLite FTS5. This matches "
        "keywords and identifiers; it is not semantic search.",
    )
    add_common(p_search)
    p_search.add_argument("--task", required=True, help="natural-language description of the task")
    p_search.add_argument(
        "--limit",
        type=_bounded_int(1, MAX_LIMIT),
        default=DEFAULT_LIMIT,
        help="maximum results (default: %(default)s)",
    )
    p_search.add_argument("--show-content", action="store_true", help="include chunk content")
    p_search.add_argument(
        "--retrieval",
        choices=MODES,
        default="bm25",
        help="bm25: lexical; bm25_graph: plus bounded graph expansion fused by reciprocal "
        "rank fusion (default: %(default)s)",
    )
    p_search.add_argument("--json", action="store_true", help="print results as JSON")
    p_search.set_defaults(func=cmd_search)

    p_pack = sub.add_parser(
        "pack",
        help="build a context package that fits a token budget",
        description="Select ranked fragments and render them as Markdown (or a JSON report) "
        "whose Markdown context never exceeds --budget tokens under the chosen encoding. "
        "All formatting overhead is counted.",
    )
    add_common(p_pack)
    p_pack.add_argument("--task", required=True, help="natural-language description of the task")
    p_pack.add_argument(
        "--budget",
        type=_bounded_int(1, MAX_BUDGET),
        required=True,
        help="maximum tokens for the packed Markdown context",
    )
    p_pack.add_argument(
        "--encoding",
        choices=sorted(SUPPORTED_ENCODINGS),
        default=DEFAULT_ENCODING,
        help="tiktoken encoding used for counting (default: %(default)s)",
    )
    p_pack.add_argument(
        "--candidates",
        type=_bounded_int(1, MAX_LIMIT),
        default=DEFAULT_CANDIDATES,
        help="number of ranked candidates to consider (default: %(default)s)",
    )
    p_pack.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="markdown: the context itself; json: full report including the context "
        "(default: %(default)s)",
    )
    p_pack.add_argument(
        "--retrieval",
        choices=MODES,
        default="bm25",
        help="candidate retrieval (default: %(default)s)",
    )
    p_pack.add_argument(
        "--strategy",
        choices=STRATEGIES,
        default="greedy",
        help="greedy: rank order; optimized: utility per token with coverage of query terms "
        "and graph symbols (default: %(default)s)",
    )
    p_pack.add_argument(
        "--memory",
        action="store_true",
        help="also consider repository memory records that are current or user decisions",
    )
    p_pack.add_argument("--memory-db", metavar="PATH", help="memory store path")
    p_pack.add_argument(
        "--trace", action="store_true", help="print the selection decision trace to stderr"
    )
    p_pack.add_argument(
        "--output",
        "-o",
        metavar="FILE",
        help="write to FILE instead of stdout, atomically; an existing regular file is "
        "replaced, a symlink or non-regular file is refused",
    )
    p_pack.set_defaults(func=cmd_pack)

    p_graph = sub.add_parser(
        "graph",
        help="show source relationships of a symbol",
        description="Show statically resolved relationships (calls, references, tests, "
        "contains, imports) for symbols matching an id, qualified name, or name. Calls "
        "through unknown receivers, getattr/importlib, and external modules are listed "
        "as uncertain, dynamic, or unresolved; they are never guessed.",
    )
    add_common(p_graph)
    p_graph.add_argument("symbol", help="symbol id (path::qualname), qualified name, or name")
    p_graph.add_argument(
        "--depth",
        type=_bounded_int(0, MAX_HOPS),
        default=1,
        help="neighborhood hops to list (default: %(default)s)",
    )
    p_graph.add_argument(
        "--limit",
        type=_bounded_int(1, 200),
        default=25,
        help="maximum neighborhood size (default: %(default)s)",
    )
    p_graph.add_argument("--json", action="store_true", help="print as JSON")
    p_graph.set_defaults(func=cmd_graph)

    p_memory = sub.add_parser(
        "memory",
        help="manage source-linked repository memory",
        description="Store short facts, conventions, failure causes, relationships, test "
        "observations, and decisions about a repository, linked to exact source lines. "
        "Freshness is re-checked against the working tree on every read.",
    )
    memory_sub = p_memory.add_subparsers(dest="memory_command", required=True, metavar="ACTION")

    def add_memory_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("repository", help="path to the repository root")
        p.add_argument(
            "--memory-db",
            metavar="PATH",
            help="memory store path (default: <repository>/.hebra/memory.sqlite3)",
        )
        p.add_argument("--json", action="store_true", help="print as JSON")

    m_add = memory_sub.add_parser("add", help="add a memory record")
    add_memory_common(m_add)
    m_add.add_argument("--kind", choices=MEMORY_KINDS, required=True, help="record type")
    m_add.add_argument("--text", required=True, help="the fact or decision")
    m_add.add_argument(
        "--ref",
        action="append",
        metavar="PATH:START[-END]",
        help="source lines supporting the record (repeatable)",
    )
    m_list = memory_sub.add_parser("list", help="list records with current freshness")
    add_memory_common(m_list)
    m_list.add_argument("--all", action="store_true", help="include invalidated records")
    m_show = memory_sub.add_parser("show", help="show one record")
    add_memory_common(m_show)
    m_show.add_argument("id", help="record id")
    m_inv = memory_sub.add_parser("invalidate", help="mark a record as no longer valid")
    add_memory_common(m_inv)
    m_inv.add_argument("id", help="record id")
    m_inv.add_argument("--reason", required=True, help="why it is no longer valid")
    p_memory.set_defaults(func=cmd_memory)

    p_stats = sub.add_parser(
        "stats",
        help="show statistics for an existing index",
        description="Summarise an existing index: files, chunks, symbols, and the last scan.",
    )
    add_common(p_stats)
    p_stats.add_argument("--json", action="store_true", help="print statistics as JSON")
    p_stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    debug = args.debug or os.environ.get("HEBRA_DEBUG") == "1"
    try:
        return int(args.func(args))
    except HebraError as exc:
        if debug:
            raise
        print(f"hebra: error: {escape_untrusted(str(exc))}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # Output piped into e.g. `head`; nothing useful left to do.
        sys.stderr.close()
        return 1
    except OSError as exc:
        if debug:
            raise
        print(f"hebra: error: {escape_untrusted(str(exc))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("hebra: interrupted; no index changes were committed", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
