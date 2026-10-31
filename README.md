<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/wordmark-dark.svg">
  <img alt="hebra." src="docs/assets/wordmark-light.svg" width="200" height="64">
</picture>

**The right context. Nothing extra.**

Hebra is a context engine for AI coding agents: given a repository, a task, and a token budget, it selects relevant code and packs it into a context document that never exceeds the budget, with the file, lines, and reason for every fragment. It runs locally, needs no API key, and never sends repository content anywhere.

Website and interactive demonstration: <https://anvitdevadiga.github.io/hebra/>

## Installation

Python 3.11 or newer.

```bash
pip install "git+https://github.com/AnvitDevadiga/hebra"
```

The only runtime dependency is [tiktoken](https://github.com/openai/tiktoken), which downloads each encoding table once (a few MB, SHA-256 verified). For offline use, populate its cache once and set `TIKTOKEN_CACHE_DIR` (see [docs/security.md](docs/security.md)).

## Quick start

```bash
cd your-repository
hebra index .
hebra search . --task "retry failed payment charges"
hebra graph . charge_with_retry
hebra pack . --task "retry failed payment charges" --budget 8000 -o context.md
hebra pack . --task "..." --budget 8000 --format json --trace   # full decision trace
hebra memory add . --kind fact --text "Charges retry 3 times" --ref shop/payments.py:12-21
hebra stats .
```

The index and memory live in `.hebra/` inside the repository and are ignored by Git.

## Example output

From the synthetic example repository in [`examples/demo-shop`](examples/demo-shop):

```text
$ hebra search . --task "retry failed payment charges" --limit 3
Query terms: retry, failed, payment, charges
Results: 3 (BM25, lexical)
  1.    5.4084  shop/payments.py:8-9  class PaymentError  [failed, payment]
  2.    4.0114  shop/payments.py:12-21  function charge_with_retry  [retry, payment]
  3.    1.8435  shop/payments.py:1-7  module  [payment]

$ hebra graph . charge_with_retry
shop/payments.py::charge_with_retry  (function, lines 12-21)
  -> references shop/payments.py::PaymentError  (line 17)
  -> calls      time.sleep  (line 20)  [unresolved: external or missing module: time]
  <- calls      shop/orders.py::checkout  (line 10)
  <- contains   file:shop/payments.py  (line 0)

$ hebra pack . --task "retry failed payment charges" --budget 600 -o context.md
hebra: packed 5 fragment(s), 564/600 tokens (o200k_base); 0 candidate(s) excluded
```

## Core capabilities

- **Safe indexing**: Git-tracked files, Python symbols via `ast`, bounded chunks, incremental transactional updates; credential-like files and symlinks excluded.
- **Source graph (Python)**: calls, references, imports, containment, and test-to-source edges, resolved statically; anything unresolvable is labelled, never guessed.
- **Retrieval**: `bm25` (default) and `bm25_graph` (one-hop expansion merged by reciprocal rank fusion). No semantic search.
- **Budgeted packing**: `greedy` (default) and experimental `optimized`; every fragment is accepted only after the whole document is re-counted with the real tokenizer.
- **Freshness**: files behind every result are re-hashed; changed files are flagged in `search` and excluded from `pack`.
- **Memory**: facts tied to source lines by hash, re-verified on every read; decisions kept separate from verified facts.

## Benchmarks

120 real fix commits from click, requests, and attrs at pinned commits; the task is the commit subject, the target is the functions the fix changed, indexed at the commit before the fix. Mean share of target functions in the packed context, 90 held-out tasks:

| | 1000 tokens | 2000 | 4000 | 8000 |
|---|---|---|---|---|
| Truncate BM25 results at the budget | 0.28 | 0.43 | 0.54 | 0.74 |
| BM25 + greedy (default) | 0.24 | 0.41 | 0.51 | 0.73 |
| BM25 + graph + greedy | 0.24 | 0.42 | 0.51 | 0.70 |
| BM25 + graph + optimized | 0.21 | 0.36 | 0.50 | 0.62 |

Graph expansion and the optimizer did not improve this metric, and simple truncation scored highest; the default therefore stays BM25 + greedy. Every run stayed within budget and every reference matched its source lines. No coding-agent experiments were run. Methodology, intervals, ablations, and raw records: [docs/benchmarks.md](docs/benchmarks.md).

## Architecture

```text
repository -> indexing (scanner, AST, SQLite FTS5) -> retrieval (BM25, graph, RRF)
           -> selection (exact token budget) -> optional memory -> Markdown + JSON trace
```

Code: `src/hebra/` (`indexing/`, `graph/`, `retrieval/`, `context/`, `memory.py`). Website: `site/`, built from engine output by `scripts/build_site_data.py`.

## Documentation

- [Retrieval and the source graph](docs/retrieval.md)
- [Context selection and the optimizer](docs/optimizer.md)
- [Memory and provenance](docs/memory.md)
- [Indexing policy and security model](docs/security.md)
- [Benchmarks and reproducibility](docs/benchmarks.md)

## Limitations

- Structural parsing and the graph cover Python only, without type inference or inheritance; other text files are indexed as line windows.
- Token counts are exact only for the chosen tiktoken encoding (`o200k_base` or `cl100k_base`).
- Whole-fragment packing can skip a large, highly ranked function; at small budgets it may pack nothing when every match is large.
- The benchmark is small and measures localization, not task success.
- Freshness checks are point-in-time; the index is not refreshed automatically.
- Filesystem-race protections narrow but do not eliminate races with a concurrent local attacker.

## Contributing

Issues and pull requests are welcome. Run the checks before opening a PR:

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
cd site && npm ci && npm test && npm run build && npm run check-links
```

If you change engine behaviour that the website shows, regenerate its data with `python scripts/build_site_data.py`; CI fails if it is stale. Never edit files in `benchmarks/results/`.

## License

MIT. See [LICENSE](LICENSE).

Built by Anvit Devadiga.

Development assistance: Claude Code was used during implementation.
