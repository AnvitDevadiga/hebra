"""Tests for the benchmark harness: labels, metrics, and record validation."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "benchmarks"
sys.path.insert(0, str(BENCH))

import mine_tasks  # noqa: E402
import run as bench_run  # noqa: E402
import summarize  # noqa: E402

LABEL = {"path": "a.py", "qualname": "f", "start_line": 10, "end_line": 20}
TASK = {"labels": [LABEL, {**LABEL, "qualname": "g", "start_line": 30, "end_line": 35}]}


def test_old_side_lines_parses_modifications_and_insertions() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -5,2 +5,3 @@\n-x\n-y\n+z\n"
        "@@ -9 +11 @@\n-w\n+v\n"
        "@@ -20,0 +23,2 @@\n+new\n"
        "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n"
    )
    assert mine_tasks.old_side_lines(diff) == {"a.py": {5, 6, 9, 20.5}}


def test_labels_map_to_innermost_definition() -> None:
    source = (
        "X = 1\n"  # 1 module level: no label
        "class A:\n"  # 2
        "    y = 2\n"  # 3 class body: label A
        "    def m(self):\n"  # 4
        "        def inner():\n"  # 5
        "            return 1\n"  # 6 -> inner
        "        return inner()\n"  # 7 -> m
    )
    labels = mine_tasks.labels_for(source, {1, 3, 6, 7, 7.5})
    assert sorted(lb["qualname"] for lb in labels) == ["A", "A.m", "A.m.<locals>.inner"]


def test_ranking_metrics_denominators() -> None:
    class Hit:
        def __init__(self, rank: int, path: str, start: int, end: int) -> None:
            self.rank, self.path, self.start_line, self.end_line = rank, path, start, end

    hits = [Hit(1, "b.py", 1, 5), Hit(2, "a.py", 15, 18), Hit(3, "a.py", 31, 31)]
    m = bench_run.ranking_metrics(TASK, hits, [1, 2, 3])
    assert m["mrr"] == 0.5 and m["first_hit_rank"] == 2
    assert (m["recall@1"], m["recall@2"], m["recall@3"]) == (0.0, 0.5, 1.0)
    assert bench_run.ranking_metrics(TASK, [], [5])["mrr"] == 0.0


def test_context_metrics_denominators() -> None:
    frags = [
        ("a.py", 1, 12, 100, frozenset({"x"}), "", "function"),  # overlaps f
        ("c.py", 1, 5, 300, frozenset({"x"}), "", "function"),  # adds no new term
        ("d.py", 1, 5, 100, frozenset({"y"}), "", "function"),
    ]
    m = bench_run.context_metrics(TASK, frags, 1000, 600)
    assert m["symbol_recall"] == 0.5 and m["any_hit"] is True
    assert m["token_precision"] == pytest.approx(100 / 500)
    assert m["redundancy"] == pytest.approx(1 / 2)
    assert m["utilization"] == 0.6 and m["budget_ok"] is True
    empty = bench_run.context_metrics(TASK, [], 1000, 50)
    assert empty["symbol_recall"] == 0.0 and empty["token_precision"] is None


def _write_run(tmp: Path, records: list[dict], tasks: list[dict], **manifest: object) -> Path:
    run_dir = tmp / "run"
    run_dir.mkdir()
    raw = "".join(json.dumps(r) + "\n" for r in records)
    (run_dir / "records.jsonl").write_text(raw)
    tasks_raw = "".join(json.dumps(t) + "\n" for t in tasks)
    (tmp / "tasks.jsonl").write_text(tasks_raw)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "records_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "tasks_sha256": hashlib.sha256(tasks_raw.encode()).hexdigest(),
                "status": "complete",
                "configs": {"bm25_greedy": {}},
                "budgets": [1000],
                "task_count": len(tasks),
                "seed": 1,
                **manifest,
            }
        )
    )
    return run_dir


def _records(task_id: str, recall: float = 1.0, budget_ok: bool = True) -> list[dict]:
    base = {
        "schema": "hebra.bench.record/v1",
        "task_id": task_id,
        "repo": "r",
        "mentions_target": False,
        "index_seconds": 0.1,
        "files_indexed": 3,
    }
    ranking = {"kind": "ranking", "mrr": 1.0, "recall@5": 1.0, "latency_ms": 1.0}
    return [
        {**base, **ranking, "config": "bm25"},
        {**base, **ranking, "config": "bm25_graph"},
        {
            **base,
            "kind": "context",
            "config": "bm25_greedy",
            "budget": 1000,
            "symbol_recall": recall,
            "any_hit": recall > 0,
            "token_precision": 0.5,
            "utilization": 0.9,
            "redundancy": 0.0,
            "references_correct": 1.0,
            "fragments": 2,
            "latency_ms": 2.0,
            "budget_ok": budget_ok,
        },
    ]


def test_summary_is_computed_from_records(tmp_path: Path) -> None:
    tasks = [{"task_id": "t1"}, {"task_id": "t2"}]
    run_dir = _write_run(tmp_path, _records("t1", 1.0) + _records("t2", 0.0), tasks)
    manifest, records, task_map = summarize.load(run_dir, tmp_path / "tasks.jsonl")
    out = summarize.summarize(manifest, records, task_map, 200)
    assert out["tasks"] == 2
    assert out["subsets"]["all"]["context"]["bm25_greedy"]["1000"]["symbol_recall"] == 0.5


@pytest.mark.parametrize("problem", ["tampered", "duplicate", "missing", "over_budget", "tasks"])
def test_invalid_records_are_rejected(tmp_path: Path, problem: str) -> None:
    tasks = [{"task_id": "t1"}, {"task_id": "t2"}]
    records = _records("t1") + _records("t2")
    if problem == "duplicate":
        records.append(records[-1])
    elif problem == "missing":
        records.pop()
    elif problem == "over_budget":
        records[-1]["budget_ok"] = False
    run_dir = _write_run(tmp_path, records, tasks)
    if problem == "tampered":
        (run_dir / "records.jsonl").write_text(
            (run_dir / "records.jsonl")
            .read_text()
            .replace('"symbol_recall": 1.0', '"symbol_recall": 0.9', 1)
        )
    if problem == "tasks":
        (tmp_path / "tasks.jsonl").write_text('{"task_id": "t1"}\n')
    with pytest.raises(summarize.ValidationError):
        manifest, recs, task_map = summarize.load(run_dir, tmp_path / "tasks.jsonl")
        summarize.summarize(manifest, recs, task_map, 50)


def test_committed_task_file_has_no_answer_leakage() -> None:
    """Retrieval input is the subject line only; it never contains the patch."""
    tasks = [json.loads(line) for line in (BENCH / "tasks" / "v1.jsonl").read_text().splitlines()]
    assert len({t["task_id"] for t in tasks}) == len(tasks)
    for t in tasks:
        assert "\n" not in t["task"] and "@@" not in t["task"] and "diff --git" not in t["task"]
        assert t["base_commit"] != t["fix_commit"]
        assert t["split"] in ("dev", "heldout")
        names = {lb["name"] for lb in t["labels"]}
        mentions = any(re.search(rf"\b{re.escape(n)}\b", t["task"]) for n in names)
        assert mentions == t["mentions_target"]
