"""Resolve raw references into a repository-wide source graph.

Runs after every index update, entirely from the database (no file reads),
so edges between files are always recomputed from the current set of
symbols: a renamed or deleted file can never leave a dangling resolved edge.

Nodes are files (``file:<path>``) and symbols (``<path>::<qualname>``, with a
``#N`` suffix for the N-th repeated definition of the same qualname in one
file). Edge kinds:

``contains``    file -> top-level symbol, class -> method, function -> nested def
``imports``     file -> imported repository module (file)
``calls``       scope -> called function/class (class call = instantiation)
``references``  scope -> referenced symbol or module, without a call
``tests``       test function -> non-test symbol it calls or references

Edge status is one of:

``resolved``    statically resolved to exactly one repository node
``ambiguous``   several repository modules match the import path
``uncertain``   ``obj.method()`` on a local value; repository methods with that
                name exist but the receiver type is unknown (no ``dst``)
``dynamic``     getattr/importlib/eval or a computed callee (no ``dst``)
``unresolved``  not found in the repository (external module, missing name,
                inherited or dynamically added attribute)

Only ``resolved`` edges are used for retrieval expansion.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_TEST_DIRS = frozenset({"tests", "test", "testing"})
_MAX_REEXPORT_DEPTH = 4


def module_names(path: str) -> list[str]:
    """Dotted module names a Python file can be imported as (src/ layout aware)."""
    pure = PurePosixPath(path)
    if pure.suffix not in (".py", ".pyi"):
        return []
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return []
    names = [".".join(parts)]
    if parts[0] in ("src", "lib") and len(parts) > 1:
        names.append(".".join(parts[1:]))
    return names


def is_test_file(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name
    return (
        any(part in _TEST_DIRS for part in pure.parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def is_test_symbol(path: str, qualname: str, kind: str) -> bool:
    if not is_test_file(path) or kind == "class":
        return False
    last = qualname.rsplit(".", 1)[-1]
    return last.startswith("test")


def assign_symbol_ids(path: str, qualnames: list[str]) -> list[str]:
    """Stable ids in definition order; repeated qualnames get ``#2``, ``#3`` ..."""
    seen: dict[str, int] = defaultdict(int)
    ids = []
    for qualname in qualnames:
        seen[qualname] += 1
        n = seen[qualname]
        ids.append(f"{path}::{qualname}" + (f"#{n}" if n > 1 else ""))
    return ids


@dataclass
class _File:
    path: str
    package: list[str]  # package parts used for relative imports
    is_package: bool
    bindings: dict[str, tuple[str, str, str | None]] = field(default_factory=dict)
    stars: list[str] = field(default_factory=list)
    symbols: dict[str, tuple[str, str]] = field(default_factory=dict)  # qualname -> (id, kind)


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str | None
    kind: str
    status: str
    target: str
    line: int
    detail: str = ""


class _Resolver:
    def __init__(self) -> None:
        self.files: dict[str, _File] = {}
        self.modules: dict[str, set[str]] = defaultdict(set)
        self.method_names: dict[str, int] = defaultdict(int)
        self.symbol_kinds: dict[str, str] = {}

    # -- import paths ---------------------------------------------------
    def absolute_module(self, f: _File, module: str) -> str | None:
        level = len(module) - len(module.lstrip("."))
        rest = module[level:]
        if level == 0:
            return rest
        base = f.package
        if level - 1 > len(base):
            return None  # relative import beyond the top-level package
        base = base[: len(base) - (level - 1)]
        return ".".join([*base, *([rest] if rest else [])]) or None

    def module_file(self, name: str) -> tuple[str, str | None]:
        """(status, path) for a dotted module name."""
        paths = self.modules.get(name, set())
        if len(paths) == 1:
            return "resolved", next(iter(paths))
        if len(paths) > 1:
            return "ambiguous", None
        return "unresolved", None

    # -- symbol lookup --------------------------------------------------
    def in_module(
        self, module: str, parts: list[str], depth: int = 0
    ) -> tuple[str, str | None, str]:
        """Resolve ``parts`` inside ``module``: (status, node id, detail)."""
        status, path = self.module_file(module)
        if path is None:
            detail = (
                "several repository modules match"
                if status == "ambiguous"
                else "external or missing module"
            )
            return status, None, f"{detail}: {module}"
        if not parts:
            return "resolved", f"file:{path}", ""
        f = self.files[path]
        found = self.member(f, parts)
        if found is not None:
            return "resolved", found, ""
        first, rest = parts[0], parts[1:]
        submodule = f"{module}.{first}"
        if submodule in self.modules:
            return self.in_module(submodule, rest, depth)
        if depth < _MAX_REEXPORT_DEPTH:
            if first in f.bindings:
                return self.via_binding(f, f.bindings[first], rest, depth + 1)
            for star in f.stars:
                result = self.in_module(star, parts, depth + 1)
                if result[0] == "resolved":
                    return result
        return "unresolved", None, f"{first!r} not defined in {module}"

    def member(self, f: _File, parts: list[str]) -> str | None:
        """Top-level symbol ``parts[0]`` in ``f``, descending into a class for ``parts[1]``."""
        top = f.symbols.get(parts[0])
        if top is None:
            return None
        if len(parts) > 1 and top[1] == "class":
            method = f.symbols.get(f"{parts[0]}.{parts[1]}")
            if method is not None:
                return method[0]
        return top[0]

    def via_binding(
        self, f: _File, binding: tuple[str, str, str | None], rest: list[str], depth: int
    ) -> tuple[str, str | None, str]:
        kind, module, name = binding
        if kind == "module":
            # `import a.b.c` binds `a`; `a.b.c.f` must resolve through the longest module.
            full = module.split(".") + rest
            for cut in range(len(full), 0, -1):
                candidate = ".".join(full[:cut])
                if candidate in self.modules:
                    return self.in_module(candidate, full[cut:], depth)
            return "unresolved", None, f"external or missing module: {module}"
        assert name is not None
        if f"{module}.{name}" in self.modules:  # `from pkg import submodule`
            return self.in_module(f"{module}.{name}", rest, depth)
        return self.in_module(module, [name, *rest], depth)

    # -- scope-aware resolution -----------------------------------------
    def visible_prefixes(self, f: _File, scope: str) -> list[str]:
        """Qualname prefixes whose definitions are visible from ``scope`` (LEGB, no class)."""
        prefixes: list[str] = []
        if scope:
            scope_kind = f.symbols.get(scope, ("", ""))[1]
            prefixes.append(scope + ("." if scope_kind == "class" else ".<locals>."))
            segments = scope.split(".<locals>.")
            for i in range(len(segments) - 1, 0, -1):
                prefixes.append(".<locals>.".join(segments[:i]) + ".<locals>.")
        prefixes.append("")
        return prefixes

    def enclosing_class(self, f: _File, scope: str) -> str | None:
        kind = f.symbols.get(scope, ("", ""))[1]
        if kind == "class":
            return scope
        if kind in ("method", "async_method") and "." in scope:
            return scope.rsplit(".", 1)[0]
        return None

    def resolve(self, f: _File, scope: str, expr: str) -> tuple[str, str | None, str]:
        parts = expr.split(".")
        root = parts[0]
        if root in ("self", "cls"):
            cls = self.enclosing_class(f, scope)
            if cls is None or len(parts) < 2:
                return "unresolved", None, "self/cls outside a method"
            method = f.symbols.get(f"{cls}.{parts[1]}")
            if method is not None:
                return "resolved", method[0], ""
            return "unresolved", None, f"{parts[1]!r} not defined on {cls} (inherited or dynamic)"
        for prefix in self.visible_prefixes(f, scope):
            local = f.symbols.get(prefix + root)
            if local is not None:
                if len(parts) > 1 and local[1] == "class":
                    method = f.symbols.get(f"{prefix}{root}.{parts[1]}")
                    if method is not None:
                        return "resolved", method[0], ""
                return "resolved", local[0], ""
        if root in f.bindings:
            return self.via_binding(f, f.bindings[root], parts[1:], 0)
        for star in f.stars:
            result = self.in_module(star, parts, 1)
            if result[0] == "resolved":
                return result
        return "unresolved", None, f"{root!r} is not a repository definition"


def rebuild_graph(conn: sqlite3.Connection) -> dict[str, int]:
    """Recompute the ``edges`` table from files, symbols, imports, and refs."""
    r = _Resolver()
    for path, module in conn.execute("SELECT path, module FROM files"):
        names = module_names(path)
        for name in names:
            r.modules[name].add(path)
        canonical = (module or "").split(".") if module else []
        is_package = PurePosixPath(path).stem == "__init__"
        package = canonical if is_package else canonical[:-1]
        r.files[path] = _File(path, package, is_package)

    for path, qualname, symbol_id, kind, name in conn.execute(
        "SELECT f.path, s.qualname, s.symbol_id, s.kind, s.name "
        "FROM symbols s JOIN files f ON f.id = s.file_id ORDER BY f.path, s.start_line, s.id"
    ):
        r.files[path].symbols.setdefault(qualname, (symbol_id, kind))
        r.symbol_kinds[symbol_id] = kind
        if kind in ("method", "async_method"):
            r.method_names[name] += 1

    for path, module, name, alias in conn.execute(
        "SELECT f.path, i.module, i.name, i.alias FROM imports i JOIN files f ON f.id = i.file_id "
        "ORDER BY f.path, i.line, i.id"
    ):
        f = r.files[path]
        if name is None:  # import a.b.c [as x]
            f.bindings[alias or module.split(".")[0]] = (
                "module",
                module if alias else module.split(".")[0],
                None,
            )
            continue
        absolute = r.absolute_module(f, module)
        if absolute is None:
            continue
        if name == "*":
            f.stars.append(absolute)
        else:
            f.bindings[alias or name] = ("from", absolute, name)

    edges: dict[tuple[str, str | None, str, str], Edge] = {}

    def add(edge: Edge) -> None:
        edges.setdefault((edge.src, edge.dst, edge.kind, edge.target), edge)

    test_ids = {
        sid
        for path, qualname, sid, kind in conn.execute(
            "SELECT f.path, s.qualname, s.symbol_id, s.kind FROM symbols s "
            "JOIN files f ON f.id = s.file_id"
        )
        if is_test_symbol(path, qualname, kind)
    }

    # contains
    for path, f in r.files.items():
        for qualname, (sid, _kind) in f.symbols.items():
            if ".<locals>." in qualname:
                parent = qualname.rsplit(".<locals>.", 1)[0]
            elif "." in qualname:
                parent = qualname.rsplit(".", 1)[0]
            else:
                parent = ""
            parent_id = f.symbols[parent][0] if parent in f.symbols else f"file:{path}"
            add(Edge(parent_id, sid, "contains", "resolved", qualname, 0))

    # imports
    for path, module, name, line in conn.execute(
        "SELECT f.path, i.module, i.name, i.line FROM imports i JOIN files f ON f.id = i.file_id "
        "ORDER BY f.path, i.line, i.id"
    ):
        f = r.files[path]
        absolute = r.absolute_module(f, module) if name is not None else module
        if absolute is None:
            add(
                Edge(
                    f"file:{path}",
                    None,
                    "imports",
                    "unresolved",
                    module,
                    line,
                    "relative import beyond the top-level package",
                )
            )
            continue
        target = absolute
        if name not in (None, "*") and f"{absolute}.{name}" in r.modules:
            target = f"{absolute}.{name}"
        status, dst_path = r.module_file(target)
        if status != "resolved" and name is None:
            # `import a.b.c`: fall back to the longest importable prefix
            pieces = target.split(".")
            for cut in range(len(pieces) - 1, 0, -1):
                status, dst_path = r.module_file(".".join(pieces[:cut]))
                if status == "resolved":
                    break
        detail = (
            ""
            if status == "resolved"
            else (
                "several repository modules match"
                if status == "ambiguous"
                else "external or missing module"
            )
        )
        dst = f"file:{dst_path}" if dst_path else None
        if dst != f"file:{path}":
            add(Edge(f"file:{path}", dst, "imports", status, target, line, detail))

    # calls / references / tests from raw references
    for path, scope, kind, expr, line in conn.execute(
        "SELECT f.path, r.scope, r.kind, r.expr, r.line FROM refs r "
        "JOIN files f ON f.id = r.file_id ORDER BY f.path, r.line, r.id"
    ):
        f = r.files[path]
        src = f.symbols[scope][0] if scope in f.symbols else f"file:{path}"
        if kind == "dynamic":
            add(
                Edge(
                    src,
                    None,
                    "calls",
                    "dynamic",
                    expr,
                    line,
                    "callee computed at run time" if expr else "callee is not a plain name",
                )
            )
            continue
        if kind == "local-call":
            attr = expr.rsplit(".", 1)[-1]
            n = r.method_names.get(attr, 0)
            if n:
                add(
                    Edge(
                        src,
                        None,
                        "calls",
                        "uncertain",
                        expr,
                        line,
                        f"receiver type unknown; {n} repository method(s) named {attr!r}",
                    )
                )
            continue
        status, dst, detail = r.resolve(f, scope, expr)
        edge_kind = "calls" if kind == "call" else "references"
        if dst == src:
            continue  # recursion / self-reference adds no relationship
        add(Edge(src, dst, edge_kind, status, expr, line, detail))
        if (
            status == "resolved"
            and dst
            and src in test_ids
            and dst not in test_ids
            and not dst.startswith("file:")
        ):
            add(Edge(src, dst, "tests", "resolved", expr, line))

    conn.execute("DELETE FROM edges")
    conn.executemany(
        "INSERT INTO edges(src, dst, kind, status, target, line, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (e.src, e.dst, e.kind, e.status, e.target, e.line, e.detail)
            for e in sorted(edges.values(), key=lambda e: (e.src, e.kind, e.dst or "", e.target))
        ],
    )
    counts: dict[str, int] = defaultdict(int)
    for e in edges.values():
        counts[e.status] += 1
    return dict(counts)
