"""Run the localization benchmark and write raw per-task records.

For each task: check out the parent of the fix commit (the fix is not in the
working tree), index it with Hebra, verify the index revision equals the
parent and that every label is a symbol in the index, then measure:

* ranking metrics for each retrieval mode (labels as relevant symbols), and
* packed-context metrics for each configuration at each budget.

Only the task text (the commit subject) is given to retrieval.

Usage:
  python benchmarks/run.py CONFIG TASKS CACHE_DIR OUT_DIR
      [--split dev|heldout|all] [--weights A,B,C] [--configs a,b] [--max-tasks N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from baselines.truncation import truncate_pack

from hebra import __version__
from hebra.config import IndexConfig, ScanConfig
from hebra.context.optimizer import Weights
from hebra.context.packer import pack_context
from hebra.indexing import index_repository
from hebra.indexing.scanner import read_repository_file
from hebra.retrieval.search import search

RECORD_SCHEMA = "hebra.bench.record/v1"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def overlaps(path: str, start: int, end: int, label: dict) -> bool:
    return path == label["path"] and start <= label["end_line"] and label["start_line"] <= end


def ranking_metrics(task: dict, hits: list, cutoffs: list[int]) -> dict:
    labels = task["labels"]
    first = next(
        (
            h.rank
            for h in hits
            if any(overlaps(h.path, h.start_line, h.end_line, lb) for lb in labels)
        ),
        None,
    )
    out: dict[str, object] = {"mrr": 0.0 if first is None else 1.0 / first, "first_hit_rank": first}
    for k in cutoffs:
        found = {
            (lb["path"], lb["qualname"])
            for lb in labels
            for h in hits[:k]
            if overlaps(h.path, h.start_line, h.end_line, lb)
        }
        out[f"recall@{k}"] = len(found) / len(labels)
    return out


def context_metrics(task: dict, fragments: list[tuple], budget: int, used: int) -> dict:
    """fragments: (path, start, end, tokens or None, terms, content, kind)."""
    labels = task["labels"]
    hit_labels = {
        (lb["path"], lb["qualname"])
        for lb in labels
        for f in fragments
        if overlaps(f[0], f[1], f[2], lb)
    }
    relevant_tokens = sum(
        f[3] or 0 for f in fragments if any(overlaps(f[0], f[1], f[2], lb) for lb in labels)
    )
    fragment_tokens = sum(f[3] or 0 for f in fragments)
    seen: set[str] = set()
    redundant = 0
    for i, f in enumerate(fragments):
        if i and f[4] <= seen:
            redundant += 1
        seen |= f[4]
    return {
        "symbol_recall": len(hit_labels) / len(labels),
        "any_hit": bool(hit_labels),
        "token_precision": (relevant_tokens / fragment_tokens) if fragment_tokens else None,
        "utilization": used / budget,
        "redundancy": (redundant / (len(fragments) - 1)) if len(fragments) > 1 else 0.0,
        "fragments": len(fragments),
        "used_tokens": used,
        "budget_ok": used <= budget,
    }


def references_correct(root: Path, fragments: list[tuple]) -> float | None:
    """Fraction of fragments whose content equals the file at the stated lines."""
    if not fragments:
        return None
    good = 0
    for path, start, end, _tokens, _terms, content, kind in fragments:
        data = read_repository_file(root, PurePosixPath(path), 50_000_000)
        if isinstance(data, str):
            continue
        text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        actual = "\n".join(lines[start - 1 : end])
        if (content in actual) if "line-part" in kind else (actual == content):
            good += 1
    return good / len(fragments)


def label_check(db: Path, task: dict) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        problems = []
        for lb in task["labels"]:
            # Qualnames can repeat (overload stubs), so match the exact span too.
            row = conn.execute(
                "SELECT 1 FROM symbols s JOIN files f ON f.id = s.file_id "
                "WHERE f.path = ? AND s.qualname = ? AND s.start_line = ? AND s.end_line = ?",
                (lb["path"], lb["qualname"], lb["start_line"], lb["end_line"]),
            ).fetchone()
            if row is None:
                problems.append(f"label not indexed as mined: {lb['path']}::{lb['qualname']}")
        return problems
    finally:
        conn.close()


def run(args: argparse.Namespace) -> int:
    config = json.loads(Path(args.config).read_text())
    ev = config["evaluation"]
    tasks_bytes = Path(args.tasks).read_bytes()
    tasks = [json.loads(line) for line in tasks_bytes.decode().splitlines()]
    if args.split != "all":
        tasks = [t for t in tasks if t["split"] == args.split]
    if args.max_tasks:
        tasks = tasks[: args.max_tasks]
    configs = ev["configs"]
    if args.configs:
        configs = {k: v for k, v in configs.items() if k in args.configs.split(",")}
    override = None
    if args.weights:
        override = Weights(*(float(x) for x in args.weights.split(",")))
    repos = {r["name"]: r for r in config["repositories"]}
    cache = Path(args.cache_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    hebra_root = Path(__file__).resolve().parent.parent
    manifest = {
        "schema": "hebra.bench.manifest/v1",
        "dataset": config["dataset"],
        "tasks_file": str(Path(args.tasks)),
        "tasks_sha256": hashlib.sha256(tasks_bytes).hexdigest(),
        "config_sha256": hashlib.sha256(Path(args.config).read_bytes()).hexdigest(),
        "split": args.split,
        "configs": configs,
        "weights_override": None if override is None else override.__dict__,
        "budgets": ev["budgets"],
        "tokenizer": ev["tokenizer"],
        "hebra_version": __version__,
        "hebra_commit": git(hebra_root, "rev-parse", "HEAD"),
        "hebra_tree_dirty": bool(git(hebra_root, "status", "--porcelain", "--", "src")),
        "repositories": {
            name: {"url": r["url"], "pinned_head": r["pinned_head"]} for name, r in repos.items()
        },
        "platform": {
            "python": platform.python_version(),
            "system": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "seed": ev["seed"],
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "running",
        "task_count": len(tasks),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    records = (out / "records.jsonl").open("w")
    index_cfg = IndexConfig(scan=ScanConfig())
    errors = 0
    for n, task in enumerate(tasks, start=1):
        repo = cache / task["repo"]
        base: dict[str, object] = {
            "schema": RECORD_SCHEMA,
            "task_id": task["task_id"],
            "repo": task["repo"],
            "split": task["split"],
            "base_commit": task["base_commit"],
            "mentions_target": task["mentions_target"],
            "labels": len(task["labels"]),
        }
        try:
            git(repo, "checkout", "-q", "--force", "--detach", task["base_commit"])
            git(repo, "clean", "-q", "-fd")  # keeps ignored files such as .hebra/
            started = time.perf_counter()
            report = index_repository(repo, config=index_cfg)
            index_seconds = time.perf_counter() - started
            if report.head_revision != task["base_commit"]:
                raise RuntimeError("index revision is not the task's base commit")
            problems = label_check(repo / ".hebra" / "index.sqlite3", task)
            if problems:
                raise RuntimeError("; ".join(problems))
            base["index_seconds"] = round(index_seconds, 4)
            base["files_indexed"] = report.files_indexed
            for mode in ("bm25", "bm25_graph"):
                started = time.perf_counter()
                response = search(repo, task["task"], limit=ev["retrieval_limit"], mode=mode)
                elapsed = time.perf_counter() - started
                records.write(
                    json.dumps(
                        {
                            **base,
                            "kind": "ranking",
                            "config": mode,
                            "latency_ms": round(elapsed * 1000, 3),
                            **ranking_metrics(task, response.hits, ev["recall_at"]),
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
            for name, spec in configs.items():
                for budget in ev["budgets"]:
                    started = time.perf_counter()
                    if spec.get("kind") == "truncation":
                        included, used = truncate_pack(
                            repo,
                            task["task"],
                            budget,
                            limit=ev["candidates"],
                            encoding=ev["tokenizer"],
                        )
                        fragments = [
                            (f.path, f.start_line, f.end_line, None, f.terms, f.content, f.kind)
                            for f in included
                        ]
                    else:
                        weights = override or Weights(
                            **{**Weights().__dict__, **spec.get("weights", {})}
                        )
                        if override and spec.get("weights"):
                            weights = Weights(**{**override.__dict__, **spec["weights"]})
                        result = pack_context(
                            repo,
                            task["task"],
                            budget,
                            encoding=ev["tokenizer"],
                            candidates=ev["candidates"],
                            retrieval=spec["retrieval"],
                            strategy=spec["strategy"],
                            weights=weights,
                        )
                        used = result.used_tokens
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
                    elapsed = time.perf_counter() - started
                    metrics = context_metrics(task, fragments, budget, used)
                    metrics["references_correct"] = references_correct(repo, fragments)
                    records.write(
                        json.dumps(
                            {
                                **base,
                                "kind": "context",
                                "config": name,
                                "budget": budget,
                                "latency_ms": round(elapsed * 1000, 3),
                                **metrics,
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
            records.flush()
        except Exception as exc:  # recorded, never silently dropped
            errors += 1
            records.write(
                json.dumps(
                    {**base, "kind": "error", "error": f"{type(exc).__name__}: {exc}"},
                    sort_keys=True,
                )
                + "\n"
            )
        print(f"[{n}/{len(tasks)}] {task['task_id']}", file=sys.stderr)
    records.close()
    manifest["status"] = "complete" if errors == 0 else f"complete with {errors} task error(s)"
    manifest["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    manifest["records_sha256"] = hashlib.sha256((out / "records.jsonl").read_bytes()).hexdigest()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"done: {len(tasks)} tasks, {errors} error(s) -> {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("tasks")
    parser.add_argument("cache_dir")
    parser.add_argument("out")
    parser.add_argument("--split", choices=("dev", "heldout", "all"), default="all")
    parser.add_argument("--weights")
    parser.add_argument("--configs")
    parser.add_argument("--max-tasks", type=int)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
