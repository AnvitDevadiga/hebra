"""Extract raw, unresolved references from a parsed Python module.

This is the per-file half of the source graph: it records *what each scope
mentions*, without knowing what other files define. Resolution against the
whole repository happens later (:mod:`hebra.graph.build`), so cross-file
edges are always recomputed from current data.

Only references that could name repository code are recorded:

* names bound at module level (definitions and imports), names of enclosing
  nested definitions, and ``self``/``cls`` inside classes;
* attribute calls on local variables and parameters (``obj.method()``), whose
  receiver type is unknown statically. Resolution keeps these only as
  *uncertain* and never turns them into edges;
* calls through ``getattr``/``importlib.import_module``/``eval`` and similar,
  recorded as *dynamic*;
* calls whose callee is not a plain dotted name (``handlers[k]()``), recorded
  as dynamic expressions.

Builtins and unknown globals are ignored unless the module uses
``from x import *`` (then bare names may come from the star import).
Scope analysis is deliberately simple: a name assigned anywhere in a function
is treated as local for the whole function, and imports inside functions are
treated as module-wide aliases.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

DYNAMIC_CALLS = frozenset(
    {
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "__import__",
        "eval",
        "exec",
        "globals",
        "locals",
        "vars",
        "importlib.import_module",
    }
)


@dataclass(frozen=True)
class RawRef:
    scope: str  # qualname of the enclosing definition; "" for module level
    kind: str  # "call", "ref", "local-call" (receiver unknown), or "dynamic"
    expr: str  # dotted expression as written, "" when not a dotted name
    line: int


def dotted_name(node: ast.AST) -> str | None:
    """``a.b.c`` for Name/Attribute chains, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _module_bindings(tree: ast.Module) -> tuple[set[str], bool]:
    """Names bound by module-level defs/classes/imports, and whether ``import *`` is used."""
    names: set[str] = set()
    star = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    star = True
                else:
                    names.add(alias.asname or alias.name)
    for node in _top_level_statements(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names, star


def _top_level_statements(tree: ast.Module) -> list[ast.stmt]:
    """Module-level statements, descending into if/try/with/for blocks."""
    out: list[ast.stmt] = []
    pending: list[ast.stmt] = list(tree.body)
    while pending:
        node = pending.pop(0)
        out.append(node)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for field in ("body", "orelse", "finalbody", "handlers"):
                for child in getattr(node, field, []) or []:
                    if isinstance(child, ast.ExceptHandler):
                        pending.extend(child.body)
                    elif isinstance(child, ast.stmt):
                        pending.append(child)
    return out


def _function_locals(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters and names stored anywhere in the function body (not nested defs)."""
    args = node.args
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    pending: list[ast.AST] = list(node.body)
    while pending:
        child = pending.pop()
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue  # their names are definitions, handled as scopes
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store | ast.Del):
            names.add(child.id)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, ast.Global | ast.Nonlocal):
            names.difference_update(child.names)
        pending.extend(ast.iter_child_nodes(child))
    return names


class _Scope:
    def __init__(self, qualname: str, kind: str, local_names: set[str], defs: set[str]) -> None:
        self.qualname = qualname
        self.kind = kind  # "module", "class", or "function"
        self.local_names = local_names
        self.defs = defs  # names of definitions directly inside this scope


def _direct_def_names(body: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    pending = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                pending.append(child)
    return names


class _Extractor(ast.NodeVisitor):
    def __init__(self, module_names: set[str], star: bool) -> None:
        self.module_names = module_names
        self.star = star
        self.scopes: list[_Scope] = [_Scope("", "module", set(), set())]
        self.refs: dict[tuple[str, str, str], RawRef] = {}

    # -- scope handling -------------------------------------------------
    def _qualname(self, name: str) -> str:
        parent = self.scopes[-1]
        if parent.kind == "module":
            return name
        if parent.kind == "class":
            return f"{parent.qualname}.{name}"
        return f"{parent.qualname}.<locals>.{name}"

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        if isinstance(node, ast.ClassDef):
            for base in (*node.bases, *(k.value for k in node.keywords)):
                self.visit(base)
            scope = _Scope(self._qualname(node.name), "class", set(), _direct_def_names(node.body))
        else:
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self.visit(default)
            scope = _Scope(
                self._qualname(node.name),
                "function",
                _function_locals(node),
                _direct_def_names(node.body),
            )
        self.scopes.append(scope)
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    visit_FunctionDef = _visit_def
    visit_AsyncFunctionDef = _visit_def
    visit_ClassDef = _visit_def

    # -- classification -------------------------------------------------
    def _current(self) -> str:
        return self.scopes[-1].qualname

    def _is_local(self, root: str) -> bool:
        for scope in reversed(self.scopes):
            if scope.kind == "function":
                if root in scope.defs:
                    return False  # a nested definition, resolvable
                if root in scope.local_names:
                    return True
        return False

    def _known(self, root: str) -> bool:
        if root in ("self", "cls"):
            return any(s.kind == "class" for s in self.scopes)
        if any(root in s.defs for s in self.scopes if s.kind == "function"):
            return True
        return root in self.module_names or self.star

    def _record(self, kind: str, expr: str, line: int) -> None:
        key = (self._current(), kind, expr)
        if key not in self.refs:
            self.refs[key] = RawRef(key[0], kind, expr, line)

    # -- references -----------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        name = dotted_name(node.func)
        if name is None:
            self._record("dynamic", "", node.lineno)
            self.visit(node.func)
        else:
            root = name.split(".")[0]
            if name in DYNAMIC_CALLS or root in DYNAMIC_CALLS:
                self._record("dynamic", name, node.lineno)
            elif self._is_local(root) and root not in ("self", "cls"):
                if "." in name:
                    self._record("local-call", name, node.lineno)
            elif self._known(root):
                self._record("call", name, node.lineno)
            # Visit receiver sub-expressions (e.g. arguments inside a.b(c).d).
            self._visit_receiver(node.func)
        for arg in (*node.args, *(k.value for k in node.keywords)):
            self.visit(arg)

    def _visit_receiver(self, func: ast.AST) -> None:
        while isinstance(func, ast.Attribute):
            func = func.value
        if not isinstance(func, ast.Name):
            self.visit(func)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = dotted_name(node)
        if name is not None and isinstance(node.ctx, ast.Load):
            root = name.split(".")[0]
            if root in self.module_names and not self._is_local(root):
                self._record("ref", name, node.lineno)
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load) or node.id in ("self", "cls"):
            return
        if self._is_local(node.id):
            return
        if node.id in self.module_names or any(
            node.id in s.defs for s in self.scopes if s.kind == "function"
        ):
            self._record("ref", node.id, node.lineno)


def extract_references(tree: ast.Module) -> list[RawRef]:
    """Raw references per scope, one per (scope, kind, expression), first line kept."""
    module_names, star = _module_bindings(tree)
    extractor = _Extractor(module_names, star)
    for statement in tree.body:
        extractor.visit(statement)
    return sorted(extractor.refs.values(), key=lambda r: (r.scope, r.line, r.kind, r.expr))
