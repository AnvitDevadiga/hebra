"""Python symbol, import, and chunk extraction using the standard ``ast`` module.

Source is parsed, never executed. Files that fail to parse (syntax errors,
pathological nesting) return ``None`` so the caller can fall back to plain
line-window chunking; that fallback carries no symbol information.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass

from hebra.config import ChunkConfig
from hebra.indexing.chunking import Chunk, split_lines, window_chunks

_DOCSTRING_MAX_CHARS = 2_000
_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


@dataclass(frozen=True)
class Symbol:
    name: str
    qualname: str
    kind: str  # function, async_function, method, async_method, class
    start_line: int
    end_line: int
    docstring: str | None


@dataclass(frozen=True)
class Import:
    module: str  # dotted module, with leading dots for relative imports
    name: str | None  # imported attribute for "from x import name"
    alias: str | None
    line: int


@dataclass(frozen=True)
class ParsedPython:
    symbols: list[Symbol]
    imports: list[Import]
    chunks: list[Chunk]


def _span(node: _DefNode) -> tuple[int, int]:
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    end = node.end_lineno or node.lineno
    return start, end


def _symbol_kind(node: _DefNode, parent_is_class: bool) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    prefix = "async_" if isinstance(node, ast.AsyncFunctionDef) else ""
    return prefix + ("method" if parent_is_class else "function")


def _child_defs(node: ast.AST) -> list[_DefNode]:
    """Definitions directly inside ``node``'s body (not nested deeper)."""
    found: list[_DefNode] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DefNode):
            found.append(child)
        elif not isinstance(child, ast.expr):
            # Defs inside if/try/with/for blocks still belong to this scope.
            found.extend(_child_defs(child))
    return found


def _collect_symbols(node: ast.AST, prefix: str, parent_is_class: bool, out: list[Symbol]) -> None:
    for child in _child_defs(node):
        qualname = f"{prefix}{child.name}"
        start, end = _span(child)
        doc = ast.get_docstring(child, clean=True)
        if doc is not None and len(doc) > _DOCSTRING_MAX_CHARS:
            doc = doc[:_DOCSTRING_MAX_CHARS]
        out.append(
            Symbol(child.name, qualname, _symbol_kind(child, parent_is_class), start, end, doc)
        )
        if isinstance(child, ast.ClassDef):
            _collect_symbols(child, f"{qualname}.", True, out)
        else:
            _collect_symbols(child, f"{qualname}.<locals>.", False, out)


def _collect_imports(tree: ast.Module) -> list[Import]:
    imports: list[Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(Import(a.name, None, a.asname, node.lineno) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.extend(Import(module, a.name, a.asname, node.lineno) for a in node.names)
    imports.sort(key=lambda i: (i.line, i.module, i.name or "", i.alias or ""))
    return imports


def _uncovered(start: int, end: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = start
    for c_start, c_end in sorted(covered):
        if c_start > cursor:
            ranges.append((cursor, c_start - 1))
        cursor = max(cursor, c_end + 1)
    if cursor <= end:
        ranges.append((cursor, end))
    return ranges


def _fits(lines: list[str], start: int, end: int, config: ChunkConfig) -> bool:
    if end - start + 1 > config.max_lines:
        return False
    return sum(len(line) + 1 for line in lines[start - 1 : end]) <= config.max_chars


def _def_chunks(
    node: _DefNode, prefix: str, parent_is_class: bool, lines: list[str], config: ChunkConfig
) -> list[Chunk]:
    qualname = f"{prefix}{node.name}"
    kind = _symbol_kind(node, parent_is_class)
    start, end = _span(node)
    if _fits(lines, start, end, config):
        return [Chunk(start, end, kind, qualname, "\n".join(lines[start - 1 : end]))]
    if not isinstance(node, ast.ClassDef):
        return window_chunks(lines, start, end, kind, qualname, config)

    # Oversized class: methods and nested classes become their own chunks,
    # the remaining class-body lines are chunked under the class name.
    chunks: list[Chunk] = []
    children = _child_defs(node)
    for child in children:
        chunks.extend(_def_chunks(child, f"{qualname}.", True, lines, config))
    for r_start, r_end in _uncovered(start, end, [_span(c) for c in children]):
        chunks.extend(window_chunks(lines, r_start, r_end, "class", qualname, config))
    return chunks


def parse_python(text: str, config: ChunkConfig) -> ParsedPython | None:
    """Parse Python source into symbols, imports and chunks, or ``None`` on failure."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. SyntaxWarning for bad escapes
            tree = ast.parse(text, type_comments=False)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None

    lines = split_lines(text)
    symbols: list[Symbol] = []
    try:
        _collect_symbols(tree, "", False, symbols)
    except RecursionError:
        return None

    chunks: list[Chunk] = []
    top_level = _child_defs(tree)
    for node in top_level:
        chunks.extend(_def_chunks(node, "", False, lines, config))
    for r_start, r_end in _uncovered(1, len(lines), [_span(n) for n in top_level]):
        chunks.extend(window_chunks(lines, r_start, r_end, "module", None, config))
    chunks.sort(key=lambda c: (c.start_line, c.end_line))

    symbols.sort(key=lambda s: (s.start_line, s.end_line, s.qualname))
    return ParsedPython(symbols, _collect_imports(tree), chunks)
