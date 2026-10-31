# hebra

**The right context. Nothing extra.**

Hebra is a local context engine for AI coding agents. Given a repository, a task, and a token budget, it finds relevant code, follows statically resolvable relationships between symbols, and packs a context document that is guaranteed to fit the budget, with every fragment's file, line range, and selection reason. It runs offline, needs no API key, and never sends repository content anywhere.

## Why

Agents do better with the code a task needs than with whole files or a guess. Most context tools cannot say what they included or why, and few check that what they show is still the current code. Hebra makes context selection inspectable: exact token accounting, source-verified references, stale-evidence detection, and a decision trace.

## Installation

Requires Python 3.11 or newer.

```bash
pip install "git+https://github.com/AnvitDevadiga/hebra"
```

The only runtime dependency is [tiktoken](https://github.com/openai/tiktoken). It downloads each encoding table once (a few MB, verified by SHA-256) and caches it; for offline machines, populate the cache once and set `TIKTOKEN_CACHE_DIR`.

## Quick start

```bash
hebra index .                                            # build or update the local index
hebra search . --task "retry failed uploads"             # ranked code, with freshness checks
hebra graph . upload_file                                # callers, callees, tests, imports
hebra memory add . --kind fact --text "Uploads retry 3 times" --ref src/upload.py:40-58
hebra pack . --task "retry failed uploads" --budget 8000 > context.md
hebra pack . --task "..." --budget 8000 --format json --trace   # full decision trace
hebra stats .
```

The index and memory live in `.hebra/` inside the repository (ignored by Git). Run `hebra <command> --help` for options.

## What it does

- **Indexing.** Git-tracked files (or a bounded filesystem walk), Python symbols via `ast`, bounded chunks, incremental updates in one transaction, credential-like files excluded. See [docs/security.md](docs/security.md).
- **Source graph.** Calls, references, imports, containment, and test-to-source edges for Python, resolved statically across the repository. Unknown receivers, `getattr`/`importlib`, and external modules are recorded as uncertain, dynamic, or unresolved, never guessed. See [docs/retrieval.md](docs/retrieval.md).
- **Retrieval.** `bm25` (SQLite FTS5, default) and `bm25_graph` (BM25 plus one-hop graph expansion, merged by reciprocal rank fusion). Both are keyword and structure based; there is no semantic or embedding search.
- **Context selection.** `greedy` (default: rank order, whole fragments) and `optimized` (experimental: a monotone submodular utility over rank, query-term coverage, and graph-symbol coverage, selected by cost-benefit greedy). Both re-count the complete rendered document with the real tokenizer before accepting any fragment, so the budget is never exceeded. See [docs/optimizer.md](docs/optimizer.md).
- **Freshness.** Every file behind a result is re-hashed against the index before it is shown; changed files are flagged in `search` and excluded from `pack`.
- **Memory.** Short source-linked facts and decisions, re-verified by content hash on every read, with provenance, branch and reset notes, and repository identity checks. Unverified or stale records never enter packed context as current. See [docs/memory.md](docs/memory.md).

## Results

On a localization benchmark of 120 real fix commits from click, requests, and attrs (task: the commit subject; label: the functions the fix changed; indexed at the parent commit), 90 held-out tasks, mean fraction of labels present in the packed context:

| | 1000 tokens | 2000 | 4000 | 8000 |
|---|---|---|---|---|
| truncation of BM25 results | 0.28 | 0.43 | 0.54 | 0.74 |
| `bm25` + `greedy` (default) | 0.24 | 0.41 | 0.51 | 0.73 |
| `bm25_graph` + `greedy` | 0.24 | 0.42 | 0.51 | 0.70 |
| `bm25_graph` + `optimized` | 0.21 | 0.36 | 0.50 | 0.62 |

The graph and the optimizer did **not** improve localization on this benchmark: graph expansion was neutral, and the optimizer was no better than rank-order greedy and worse at 8000 tokens. Simple truncation did best. Every packed context stayed within budget and every reference matched its source lines. Methodology, paired confidence intervals, ablations, the diagnosis, and the pre-registered decision to keep `greedy` as the default are in [docs/benchmarks.md](docs/benchmarks.md). No coding-agent experiments have been run, so nothing here shows that agents succeed more often.

## Limitations

- Retrieval is lexical and structural, not semantic. Graph resolution covers Python only, without type inference or inheritance.
- Token counts are exact only for the chosen tiktoken encoding (`o200k_base` or `cl100k_base`); other tokenizers, including Claude's, count differently.
- `greedy` packs whole fragments, so a large, highly ranked function that does not fit is skipped; truncation's advantage above comes from that.
- The benchmark is small (three repositories, 120 tasks, a 30-task tuning split), measures localization rather than task success, and uses changed functions as a proxy for needed context.
- Freshness checks are point-in-time; the index is not refreshed automatically.
- Filesystem-race protections narrow but do not eliminate races with a concurrent local attacker; see [docs/security.md](docs/security.md).

## Reproducibility

`benchmarks/` contains the pinned repository list, the task miner (which reproduces the task file byte for byte), the runner, the summarizer (which validates record hashes before computing anything), raw per-task records for every run, and the memory-freshness scenarios. See [docs/benchmarks.md](docs/benchmarks.md#reproducing).

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
python -m build
```

## License

MIT. See [LICENSE](LICENSE).

Created and maintained by Anvit Devadiga.

Development assistance: Claude Code was used during implementation.
