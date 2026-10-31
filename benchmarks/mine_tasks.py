"""Mine fix-commit localization tasks from pinned repositories.

Rule (deterministic; parameters in the config's "mining" section):

1. Walk first-parent, non-merge commits reachable from the pinned head,
   newest first, scanning at most ``max_commits_scanned``.
2. Keep a commit when its subject has at least ``min_subject_words`` words and
   does not match ``excluded_subject_pattern``, and it modifies between 1 and
   ``max_source_files_changed`` non-test Python files that Hebra would index
   (test files and excluded path prefixes are ignored when counting).
3. Labels: on the parent commit, map every removed/modified line (and every
   insertion point) of those files to the innermost function, method, or
   class whose span contains it. Module-level lines map to nothing. Keep the
   commit if it yields between ``min_target_symbols`` and
   ``max_target_symbols`` labels.
4. Stop after ``max_tasks_per_repo`` tasks per repository.

The task text is the commit subject only. ``mentions_target`` records whether
any label's name appears in it as a whole word. The split is ``dev`` when the
first 8 hex digits of sha256(task_id) modulo 10 is below
``dev_fraction_per_10``, else ``heldout``.

Usage: python benchmarks/mine_tasks.py CONFIG CACHE_DIR OUTPUT_JSONL
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from hebra.config import ChunkConfig, ScanConfig
from hebra.graph.build import is_test_file
from hebra.indexing.python_parser import parse_python
from hebra.indexing.scanner import _is_excluded, is_sensitive_name

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
        errors="replace",
    ).stdout


def ensure_clone(cache: Path, name: str, url: str, head: str) -> Path:
    repo = cache / name
    if not repo.exists():
        subprocess.run(["git", "clone", "-q", url, str(repo)], check=True)
    if subprocess.run(["git", "cat-file", "-e", f"{head}^{{commit}}"], cwd=repo).returncode:
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=repo, check=True)
    return repo


def is_source(path: str, prefixes: list[str]) -> bool:
    scan = ScanConfig()
    rel = PurePosixPath(path)
    return (
        path.endswith(".py")
        and not is_test_file(path)
        and not any(path.startswith(p) for p in prefixes)
        and not _is_excluded(rel, scan)
        and not is_sensitive_name(rel.name, scan)
    )


def old_side_lines(diff: str) -> dict[str, set[float]]:
    """Per old-side path: modified line numbers, and insertion points as n + 0.5."""
    touched: dict[str, set[float]] = {}
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("@@") and path is not None:
            match = _HUNK.match(line)
            if match is None:
                continue
            start, count = int(match[1]), int(match[2] if match[2] is not None else 1)
            lines = touched.setdefault(path, set())
            if count == 0:
                lines.add(start + 0.5)
            else:
                lines.update(range(start, start + count))
    return touched


def labels_for(source: str, lines: set[float]) -> list[dict[str, object]]:
    parsed = parse_python(source, ChunkConfig())
    if parsed is None:
        return []
    found: dict[str, dict[str, object]] = {}
    for line in sorted(lines):
        containing = [s for s in parsed.symbols if s.start_line <= line <= s.end_line]
        if not containing:
            continue
        inner = min(containing, key=lambda s: (s.end_line - s.start_line, -s.start_line))
        found[inner.qualname] = {
            "qualname": inner.qualname,
            "name": inner.name,
            "kind": inner.kind,
            "start_line": inner.start_line,
            "end_line": inner.end_line,
        }
    return list(found.values())


def mine(config: dict, cache: Path) -> list[dict[str, object]]:
    rules = config["mining"]
    excluded = re.compile(rules["excluded_subject_pattern"], re.IGNORECASE)
    tasks: list[dict[str, object]] = []
    for spec in config["repositories"]:
        repo = ensure_clone(cache, spec["name"], spec["url"], spec["pinned_head"])
        shas = git(
            repo,
            "rev-list",
            "--first-parent",
            "--no-merges",
            f"--max-count={rules['max_commits_scanned']}",
            spec["pinned_head"],
        ).split()
        kept = 0
        for sha in shas:
            if kept >= rules["max_tasks_per_repo"]:
                break
            subject = git(repo, "log", "-1", "--format=%s", sha).strip()
            if len(subject.split()) < rules["min_subject_words"] or excluded.match(subject):
                continue
            parents = git(repo, "log", "-1", "--format=%P", sha).split()
            if len(parents) != 1:
                continue
            parent = parents[0]
            changed = [
                p
                for p in git(repo, "diff", "--name-only", "--no-renames", parent, sha).split("\n")
                if p
            ]
            sources = [p for p in changed if is_source(p, rules["excluded_path_prefixes"])]
            if not 1 <= len(sources) <= rules["max_source_files_changed"]:
                continue
            diff = git(repo, "diff", "-U0", "--no-renames", parent, sha, "--", *sources)
            labels: list[dict[str, object]] = []
            for path, lines in sorted(old_side_lines(diff).items()):
                try:
                    source = git(repo, "show", f"{parent}:{path}")
                except subprocess.CalledProcessError:
                    continue  # file added by the commit: nothing to localize
                labels.extend({"path": path, **label} for label in labels_for(source, lines))
            if not rules["min_target_symbols"] <= len(labels) <= rules["max_target_symbols"]:
                continue
            names = {str(label["name"]) for label in labels}
            mentions = any(re.search(rf"\b{re.escape(n)}\b", subject) for n in names)
            task_id = f"{spec['name']}@{sha[:12]}"
            digest = int(hashlib.sha256(task_id.encode()).hexdigest()[:8], 16)
            tasks.append(
                {
                    "task_id": task_id,
                    "repo": spec["name"],
                    "fix_commit": sha,
                    "base_commit": parent,
                    "task": subject,
                    "labels": labels,
                    "mentions_target": mentions,
                    "split": "dev" if digest % 10 < rules["dev_fraction_per_10"] else "heldout",
                }
            )
            kept += 1
    return tasks


def main() -> int:
    config_path, cache, output = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    config = json.loads(config_path.read_text())
    cache.mkdir(parents=True, exist_ok=True)
    tasks = mine(config, cache)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(t, sort_keys=True) + "\n" for t in tasks))
    by_split: dict[str, int] = {}
    for t in tasks:
        by_split[str(t["split"])] = by_split.get(str(t["split"]), 0) + 1
    print(f"{len(tasks)} tasks -> {output} {by_split}")
    print("sha256:", hashlib.sha256(output.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
