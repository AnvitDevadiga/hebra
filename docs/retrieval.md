# Source graph and retrieval

## Source graph (Python)

During `hebra index`, each Python file's AST yields its definitions (files,
modules, classes, functions, async functions, methods, test functions) and the
names each scope mentions. After every index update, those references are
resolved against the whole index, entirely from the database, so edges between
files are always recomputed from current data. A renamed or deleted file
cannot leave a dangling resolved edge.

Symbol ids are `path::qualname`, for example `src/pkg/core.py::Cache.get` or
`src/pkg/core.py::outer.<locals>.helper`. A qualname defined more than once in
a file (overload stubs, property setters, conditional definitions) gets `#2`,
`#3`, ... in definition order; references resolve to the last definition, as
Python's later binding would.

| edge | meaning |
|---|---|
| `contains` | file to top-level symbol, class to method, function to nested definition |
| `imports` | file to an imported repository module |
| `calls` | scope to a called function or class (calling a class is instantiation) |
| `references` | scope to a referenced symbol or module, without a call |
| `tests` | test function to a non-test symbol it calls or references |

What resolves: same-module and nested-scope names (Python scoping: class bodies
are not visible from methods), `self.x` / `cls.x` to methods of the enclosing
class, `import a.b`, `import a.b as x`, `from m import n as y`, relative imports,
`src/` layouts, re-exports through package `__init__` files (bounded depth),
and `from m import *`.

What does not, and how it is recorded instead:

| status | example |
|---|---|
| `uncertain` | `obj.method()` on a local value, when repository methods with that name exist; the receiver type is unknown, so no edge target is claimed |
| `dynamic` | `getattr(x, name)()`, `importlib.import_module(...)`, `eval`, `handlers[k]()`, `Cart().add()` |
| `unresolved` | external or missing modules, names not defined in the target module, attributes inherited from base classes |
| `ambiguous` | an import path matching more than one repository module |

There is no type inference: a call on the result of another call, or on a
parameter, is never attributed to a specific method. Only `resolved` edges are
used for retrieval. `hebra graph <repo> <symbol>` shows all of a symbol's edges,
including the unresolved ones.

Limits: one scope analysis per function (a name assigned anywhere in a function
is local to all of it); imports inside functions are treated as module-wide;
inheritance is not followed.

## Retrieval modes

### `bm25` (default)

SQLite FTS5 `bm25()` over four columns: file path, symbol names, camel-case
identifier parts, and code. Column weights are 2, 3, 1, 1 (untuned). The score
is the negated FTS5 value: higher is better, and it is comparable within one
query only. This is keyword matching, not semantic search.

### `bm25_graph`

1. Run BM25 with a pool of 50.
2. Seeds: the symbols defined in the top 10 BM25 hits.
3. Graph list: those 10 hits first (hop 0), then chunks defining symbols at
   most one resolved edge from a seed, ordered by the BM25 rank of the seed,
   then by edge priority (callees, referenced symbols, callers, references,
   tests, containment; source before tests), at most 40 symbols.
4. Fuse the BM25 list and the graph list with reciprocal rank fusion
   (Cormack et al. 2009): `score(c) = sum over lists of 1 / (60 + rank)`.

Only ranks are combined, never raw scores from different methods. Because the
top 10 BM25 hits head both lists, they keep their BM25 order; graph expansion
can only reorder what follows. Each hit records its BM25 rank, graph rank,
fused score, and the edges that reached it (`--json`, or `hebra pack --trace`).

### Not implemented: semantic retrieval

There is no embedding-based retrieval. A `hybrid` mode would need a local
embedding model and its dependencies, and nothing here would justify calling
graph expansion "semantic".

## Freshness

Every retrieved result's source file is re-hashed and compared with the
indexed snapshot before anything is shown. Stale results are marked in
`search` and excluded from `pack`.
