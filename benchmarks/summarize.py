"""Validate raw benchmark records and derive summary tables from them.

Everything in the summary is computed from ``records.jsonl``; nothing is
entered by hand. Validation fails loudly when the raw data is inconsistent:
hash mismatches, duplicate or missing records, or any budget violation.

Metrics (per task, then averaged over tasks with equal weight):

symbol_recall      labels whose span overlaps a packed fragment / labels
any_hit            at least one label overlapped
token_precision    tokens of fragments overlapping a label / fragment tokens
                   (not defined for truncation, which has no per-fragment counts)
utilization        used tokens / budget
redundancy         fragments adding no query term beyond earlier fragments /
                   (fragments - 1)
references_correct fragments whose text equals the file at the stated lines
recall@k, mrr      ranking metrics over the first 50 retrieved chunks

Paired comparisons use the same tasks for both configurations and report the
mean per-task difference with a percentile bootstrap 95% interval (seeded).

Usage: python benchmarks/summarize.py RUN_DIR TASKS [RUN_DIR ...]
"""

from __future__ import annotations

import hashlib
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

COMPARISONS = [
    ("hebra_optimized", "bm25_greedy"),
    ("hebra_optimized", "truncation"),
    ("hebra_optimized", "graph_greedy"),
    ("graph_greedy", "bm25_greedy"),
    ("bm25_greedy", "truncation"),
    ("ablation_no_graph", "hebra_optimized"),
    ("ablation_no_coverage", "hebra_optimized"),
]


class ValidationError(Exception):
    pass


def load(run_dir: Path, tasks_path: Path) -> tuple[dict, list[dict], dict[str, dict]]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    raw = (run_dir / "records.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest.get("records_sha256"):
        raise ValidationError("records.jsonl does not match the manifest hash")
    tasks_bytes = tasks_path.read_bytes()
    if hashlib.sha256(tasks_bytes).hexdigest() != manifest["tasks_sha256"]:
        raise ValidationError("tasks file does not match the manifest hash")
    if not str(manifest.get("status", "")).startswith("complete"):
        raise ValidationError(f"run is not complete: {manifest.get('status')}")
    tasks = {t["task_id"]: t for t in map(json.loads, tasks_bytes.decode().splitlines())}
    records = [json.loads(line) for line in raw.decode().splitlines()]
    return manifest, records, tasks


def validate(manifest: dict, records: list[dict], tasks: dict[str, dict]) -> tuple[set, set]:
    seen = set()
    ok_tasks: set[str] = set()
    error_tasks: set[str] = set()
    for r in records:
        if r.get("schema") != "hebra.bench.record/v1":
            raise ValidationError(f"bad record schema: {r.get('schema')}")
        if r["task_id"] not in tasks:
            raise ValidationError(f"record for unknown task {r['task_id']}")
        key = (r["task_id"], r["kind"], r.get("config"), r.get("budget"))
        if key in seen:
            raise ValidationError(f"duplicate record {key}")
        seen.add(key)
        if r["kind"] == "error":
            error_tasks.add(r["task_id"])
        else:
            ok_tasks.add(r["task_id"])
        if r["kind"] == "context" and not r["budget_ok"]:
            raise ValidationError(f"budget exceeded: {key}")
    ok_tasks -= error_tasks
    for task_id in ok_tasks:
        for mode in ("bm25", "bm25_graph"):
            if (task_id, "ranking", mode, None) not in seen:
                raise ValidationError(f"missing ranking record {task_id} {mode}")
        for config in manifest["configs"]:
            for budget in manifest["budgets"]:
                if (task_id, "context", config, budget) not in seen:
                    raise ValidationError(f"missing context record {task_id} {config} {budget}")
    if len(ok_tasks) + len(error_tasks) != manifest["task_count"]:
        raise ValidationError("record task count differs from the manifest")
    return ok_tasks, error_tasks


def mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def bootstrap(diffs: list[float], samples: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(statistics.fmean(rng.choices(diffs, k=n)) for _ in range(samples))
    return round(means[int(0.025 * samples)], 4), round(means[int(0.975 * samples) - 1], 4)


def summarize(manifest: dict, records: list[dict], tasks: dict[str, dict], samples: int) -> dict:
    ok_tasks, error_tasks = validate(manifest, records, tasks)
    usable = [r for r in records if r["task_id"] in ok_tasks]
    subsets = {
        "all": lambda r: True,
        "subject_does_not_name_a_label": lambda r: not r["mentions_target"],
    }
    out: dict = {
        "tasks": len(ok_tasks),
        "task_errors": sorted(error_tasks),
        "subsets": {},
        "indexing": {},
    }
    for subset, keep in subsets.items():
        rows = [r for r in usable if keep(r)]
        task_ids = sorted({r["task_id"] for r in rows})
        section: dict = {"tasks": len(task_ids), "ranking": {}, "context": {}, "paired": {}}
        for mode in ("bm25", "bm25_graph"):
            rs = [r for r in rows if r["kind"] == "ranking" and r["config"] == mode]
            metrics: dict[str, float | None] = {}
            if rs:
                for k in sorted(rs[0]):
                    if k.startswith("recall@"):
                        metrics[k] = mean([r[k] for r in rs])
                metrics["mrr"] = mean([r["mrr"] for r in rs])
                metrics["latency_ms_median"] = round(
                    statistics.median([r["latency_ms"] for r in rs]), 2
                )
            section["ranking"][mode] = metrics
        by_key: dict[tuple, dict[str, dict]] = defaultdict(dict)
        for r in rows:
            if r["kind"] == "context":
                by_key[(r["config"], r["budget"])][r["task_id"]] = r
        for (config, budget), per_task in sorted(by_key.items()):
            rs = list(per_task.values())
            precision = [r["token_precision"] for r in rs if r["token_precision"] is not None]
            refs = [r["references_correct"] for r in rs if r["references_correct"] is not None]
            section["context"].setdefault(config, {})[str(budget)] = {
                "symbol_recall": mean([r["symbol_recall"] for r in rs]),
                "any_hit": mean([float(r["any_hit"]) for r in rs]),
                "token_precision": mean(precision),
                "utilization": mean([r["utilization"] for r in rs]),
                "redundancy": mean([r["redundancy"] for r in rs]),
                "references_correct": mean(refs),
                "fragments": mean([r["fragments"] for r in rs]),
                "latency_ms_median": round(statistics.median([r["latency_ms"] for r in rs]), 2),
            }
        for a, b in COMPARISONS:
            if a not in manifest["configs"] or b not in manifest["configs"]:
                continue
            for budget in manifest["budgets"]:
                pa, pb = by_key[(a, budget)], by_key[(b, budget)]
                diffs = [pa[t]["symbol_recall"] - pb[t]["symbol_recall"] for t in task_ids]
                if not diffs:
                    continue
                low, high = bootstrap(diffs, samples, manifest["seed"])
                section["paired"].setdefault(f"{a} vs {b}", {})[str(budget)] = {
                    "mean_recall_difference": mean(diffs),
                    "ci95": [low, high],
                    "wins": sum(d > 0 for d in diffs),
                    "ties": sum(d == 0 for d in diffs),
                    "losses": sum(d < 0 for d in diffs),
                }
        out["subsets"][subset] = section
    by_repo: dict[str, list[float]] = defaultdict(list)
    first: dict[str, float] = {}  # first task's index time; incremental if a cache index existed
    for r in sorted(usable, key=lambda r: r["task_id"]):
        if r["kind"] == "ranking" and r["config"] == "bm25":
            by_repo[r["repo"]].append(r["index_seconds"])
    for r in records:
        if r["kind"] == "ranking" and r["config"] == "bm25" and r["repo"] not in first:
            first[r["repo"]] = r["index_seconds"]
    for repo, values in sorted(by_repo.items()):
        out["indexing"][repo] = {
            "first_task_index_seconds": first.get(repo),
            "median_incremental_seconds": round(statistics.median(values), 4),
            "files_indexed_median": statistics.median(
                [
                    r["files_indexed"]
                    for r in usable
                    if r["repo"] == repo and r["kind"] == "ranking" and r["config"] == "bm25"
                ]
            ),
        }
    return out


def markdown(summary: dict, manifest: dict) -> str:
    lines = [
        f"# Benchmark summary: {manifest['dataset']} ({manifest['split']} split)",
        "",
        f"Generated by `benchmarks/summarize.py` from `records.jsonl`. Hebra "
        f"{manifest['hebra_version']} at `{manifest['hebra_commit'][:12]}`"
        f"{' (dirty tree)' if manifest['hebra_tree_dirty'] else ''}; tokenizer "
        f"{manifest['tokenizer']}; {summary['tasks']} task(s); task errors: "
        f"{len(summary['task_errors'])}.",
        "",
    ]
    for subset, section in summary["subsets"].items():
        lines += [f"## Subset: {subset} ({section['tasks']} tasks)", "", "### Ranking (top 50)", ""]
        lines += [
            "| mode | recall@5 | recall@10 | recall@20 | MRR | median ms |",
            "|---|---|---|---|---|---|",
        ]
        for mode, m in section["ranking"].items():
            lines.append(
                f"| {mode} | {m.get('recall@5')} | {m.get('recall@10')} | {m.get('recall@20')} "
                f"| {m['mrr']} | {m['latency_ms_median']} |"
            )
        lines += ["", "### Packed context: mean symbol recall by budget", ""]
        budgets = [str(b) for b in manifest["budgets"]]
        lines += ["| config | " + " | ".join(budgets) + " |", "|---|" + "---|" * len(budgets)]
        for config, per_budget in section["context"].items():
            lines.append(
                f"| {config} | "
                + " | ".join(str(per_budget[b]["symbol_recall"]) for b in budgets)
                + " |"
            )
        lines += ["", "### Packed context at 4000 tokens", ""]
        lines += [
            "| config | recall | any hit | token precision | utilization | redundancy "
            "| refs correct | median ms |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for config, per_budget in section["context"].items():
            m = per_budget.get("4000")
            if m:
                lines.append(
                    f"| {config} | {m['symbol_recall']} | {m['any_hit']} | {m['token_precision']} "
                    f"| {m['utilization']} | {m['redundancy']} | {m['references_correct']} "
                    f"| {m['latency_ms_median']} |"
                )
        lines += ["", "### Paired symbol-recall differences (A minus B)", ""]
        lines += [
            "| A vs B | budget | mean diff | 95% CI | wins/ties/losses |",
            "|---|---|---|---|---|",
        ]
        for pair, per_budget in section["paired"].items():
            for budget, m in per_budget.items():
                lines.append(
                    f"| {pair} | {budget} | {m['mean_recall_difference']} | "
                    f"[{m['ci95'][0]}, {m['ci95'][1]}] | {m['wins']}/{m['ties']}/{m['losses']} |"
                )
        lines.append("")
    lines += [
        "## Indexing time per task",
        "",
        "Index time when switching to each task's base commit; the first task re-indexes "
        "from whatever index the cached clone already had. Cold full-index times are "
        "measured separately by `benchmarks/index_timing.py`.",
        "",
        "| repo | first task s | median per task s | files |",
        "|---|---|---|---|",
    ]
    for repo, m in summary["indexing"].items():
        lines.append(
            f"| {repo} | {m['first_task_index_seconds']} | {m['median_incremental_seconds']} "
            f"| {m['files_indexed_median']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    run_dir, tasks_path = Path(sys.argv[1]), Path(sys.argv[2])
    manifest, records, tasks = load(run_dir, tasks_path)
    samples = 2000
    summary = summarize(manifest, records, tasks, samples)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (run_dir / "summary.md").write_text(markdown(summary, manifest))
    print((run_dir / "summary.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
