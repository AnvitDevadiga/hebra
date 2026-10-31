"""Choose optimizer weights on the dev split only.

Grid: alpha in {0.5, 1, 2}, beta in {0.5, 1, 2}, gamma in {0, 0.5, 1, 2}.
Each dev task is indexed once at its base commit; every weight setting is
then packed at every budget with ``bm25_graph`` retrieval and the
``optimized`` strategy. Selection rule, fixed before looking at results:
highest mean symbol recall over all (task, budget) pairs; ties go to the
setting closest to (1, 1, 1) in L1 distance, then lexicographically smallest.
Held-out tasks are never read.

Usage: python benchmarks/tune.py CONFIG TASKS CACHE_DIR OUT_DIR
"""

from __future__ import annotations

import hashlib
import itertools
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import context_metrics, git, label_check

from hebra.config import IndexConfig, ScanConfig
from hebra.context.optimizer import Weights
from hebra.context.packer import pack_context
from hebra.indexing import index_repository

GRID = list(itertools.product((0.5, 1.0, 2.0), (0.5, 1.0, 2.0), (0.0, 0.5, 1.0, 2.0)))


def main() -> int:
    config_path, tasks_path, cache, out = (Path(a) for a in sys.argv[1:5])
    config = json.loads(config_path.read_text())
    ev = config["evaluation"]
    tasks_bytes = tasks_path.read_bytes()
    tasks = [t for t in map(json.loads, tasks_bytes.decode().splitlines()) if t["split"] == "dev"]
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for task in tasks:
        repo = cache / task["repo"]
        git(repo, "checkout", "-q", "--force", "--detach", task["base_commit"])
        git(repo, "clean", "-q", "-fd")
        report = index_repository(repo, config=IndexConfig(scan=ScanConfig()))
        assert report.head_revision == task["base_commit"]
        assert not label_check(repo / ".hebra" / "index.sqlite3", task)
        for a, b, c in GRID:
            for budget in ev["budgets"]:
                result = pack_context(
                    repo,
                    task["task"],
                    budget,
                    encoding=ev["tokenizer"],
                    candidates=ev["candidates"],
                    retrieval="bm25_graph",
                    strategy="optimized",
                    weights=Weights(a, b, c),
                )
                fragments = [
                    (
                        f.hit.path,
                        f.hit.start_line,
                        f.hit.end_line,
                        f.tokens,
                        frozenset(f.hit.matched_terms),
                        f.hit.content,
                        f.hit.kind,
                    )
                    for f in result.selected
                ]
                m = context_metrics(task, fragments, budget, result.used_tokens)
                records.append(
                    {
                        "task_id": task["task_id"],
                        "weights": [a, b, c],
                        "budget": budget,
                        "symbol_recall": m["symbol_recall"],
                        "budget_ok": m["budget_ok"],
                    }
                )
        print(f"tuned on {task['task_id']}", file=sys.stderr)
    assert all(r["budget_ok"] for r in records)
    table = []
    for a, b, c in GRID:
        values = [r["symbol_recall"] for r in records if r["weights"] == [a, b, c]]
        table.append(
            {"weights": [a, b, c], "mean_symbol_recall": round(statistics.fmean(values), 6)}
        )
    best = max(
        table,
        key=lambda row: (
            row["mean_symbol_recall"],
            -sum(abs(w - 1.0) for w in row["weights"]),
            [-w for w in row["weights"]],
        ),
    )
    raw = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    (out / "records.jsonl").write_text(raw)
    summary = {
        "schema": "hebra.bench.tuning/v1",
        "split": "dev",
        "tasks": len(tasks),
        "tasks_sha256": hashlib.sha256(tasks_bytes).hexdigest(),
        "records_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "hebra_commit": git(Path(__file__).resolve().parent.parent, "rev-parse", "HEAD"),
        "budgets": ev["budgets"],
        "rule": "max mean symbol recall; ties: closest to (1,1,1) in L1, then smallest",
        "grid": sorted(table, key=lambda r: -r["mean_symbol_recall"]),
        "chosen": best,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out / "tuning.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"chosen": best, "top5": summary["grid"][:5], "worst": summary["grid"][-1]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
