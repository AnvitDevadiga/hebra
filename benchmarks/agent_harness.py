"""Optional end-to-end agent benchmark harness (no results have been recorded).

Retrieval metrics do not show that a coding agent succeeds more often. This
harness prepares what an agent experiment needs and runs one only when the
operator supplies an agent command and accepts its cost explicitly.

Per task it:

1. checks out the task's base commit and indexes it;
2. writes ``prompt.md``: the task text, Hebra's packed context, and a fixed
   instruction to answer with a unified diff;
3. if ``--runner`` is given, runs that command with the prompt path as its only
   argument (no shell), with a timeout, and saves stdout as ``patch.diff``;
4. records whether the patch applies to the base commit (``git apply --check``)
   and, if ``--test-cmd`` is given, the exit status of that command after
   applying the patch.

Every attempt is a record: runner failures, timeouts, empty output, and
non-applying patches are recorded as such and never counted as successes.
Provider, model, token counts, and cost are whatever the runner reports on
stderr as a JSON line ``{"provider": ..., "model": ..., "input_tokens": ...,
"output_tokens": ..., "cost_usd": ...}``; they are stored as reported.

Running a runner requires ``--max-tasks`` and ``--i-accept-the-cost``. No
runner is configured in this repository and no agent results exist.

Usage:
  python benchmarks/agent_harness.py CONFIG TASKS CACHE_DIR OUT_DIR --budget 8000
      [--split heldout] [--runner CMD --max-tasks N --i-accept-the-cost]
      [--test-cmd CMD] [--timeout 900]
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import git

from hebra.context.packer import pack_context
from hebra.indexing import index_repository

INSTRUCTION = (
    "You are fixing a bug in the repository described below. Answer with a single "
    "unified diff that applies with `git apply` at the repository root, and nothing else."
)


def reported_usage(stderr: str) -> dict:
    for line in reversed(stderr.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "model" in data:
            return {
                k: data.get(k)
                for k in ("provider", "model", "input_tokens", "output_tokens", "cost_usd")
            }
    return {}


def main() -> int:
    p = argparse.ArgumentParser()
    for name in ("config", "tasks", "cache_dir", "out"):
        p.add_argument(name)
    p.add_argument("--budget", type=int, required=True)
    p.add_argument("--split", default="heldout")
    p.add_argument("--runner")
    p.add_argument("--max-tasks", type=int)
    p.add_argument("--i-accept-the-cost", action="store_true")
    p.add_argument("--test-cmd")
    p.add_argument("--timeout", type=int, default=900)
    args = p.parse_args()
    if args.runner and not (args.max_tasks and args.i_accept_the_cost):
        p.error("--runner requires --max-tasks and --i-accept-the-cost")

    tasks = [
        t
        for t in map(json.loads, Path(args.tasks).read_text().splitlines())
        if t["split"] == args.split
    ]
    if args.max_tasks:
        tasks = tasks[: args.max_tasks]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    records = (out / "records.jsonl").open("w")
    for task in tasks:
        repo = Path(args.cache_dir) / task["repo"]
        task_dir = out / task["task_id"].replace("@", "_")
        task_dir.mkdir(exist_ok=True)
        git(repo, "checkout", "-q", "--force", "--detach", task["base_commit"])
        git(repo, "clean", "-q", "-fd")
        index_repository(repo)
        packed = pack_context(repo, task["task"], args.budget)
        prompt = f"{INSTRUCTION}\n\n## Task\n\n{task['task']}\n\n{packed.markdown}"
        (task_dir / "prompt.md").write_text(prompt)
        record: dict = {
            "task_id": task["task_id"],
            "base_commit": task["base_commit"],
            "budget": args.budget,
            "context_tokens": packed.used_tokens,
            "runner": args.runner,
            "status": "prompt-only",
        }
        if args.runner:
            started = time.perf_counter()
            try:
                result = subprocess.run(
                    [*shlex.split(args.runner), str(task_dir / "prompt.md")],
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                record["status"] = "runner-timeout"
            else:
                record.update(reported_usage(result.stderr))
                record["runner_exit"] = result.returncode
                (task_dir / "patch.diff").write_text(result.stdout)
                if result.returncode != 0:
                    record["status"] = "runner-error"
                elif not result.stdout.strip():
                    record["status"] = "no-patch"
                else:
                    applies = (
                        subprocess.run(
                            ["git", "apply", "--check", str(task_dir / "patch.diff")],
                            cwd=repo,
                            capture_output=True,
                            check=False,
                        ).returncode
                        == 0
                    )
                    record["status"] = "patch-applies" if applies else "patch-does-not-apply"
                    if applies and args.test_cmd:
                        subprocess.run(
                            ["git", "apply", str(task_dir / "patch.diff")], cwd=repo, check=True
                        )
                        tests = subprocess.run(
                            shlex.split(args.test_cmd),
                            cwd=repo,
                            capture_output=True,
                            timeout=args.timeout,
                            check=False,
                        )
                        record["tests_exit"] = tests.returncode
                        record["status"] = "tests-pass" if tests.returncode == 0 else "tests-fail"
            record["latency_s"] = round(time.perf_counter() - started, 3)
        record["recorded_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        records.write(json.dumps(record, sort_keys=True) + "\n")
    records.close()
    print(f"{len(tasks)} task(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
