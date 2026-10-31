"""Memory freshness correctness on constructed scenarios.

Each scenario builds a small Git repository, adds a memory record, applies one
change, and compares the record's computed status (and the reference status)
with the status the scenario was constructed to produce. Labels are known by
construction, so this measures whether freshness detection implements its
documented semantics; it says nothing about whether memory helps an agent.

Usage: python benchmarks/memory_freshness.py OUT_DIR
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from hebra.config import IndexConfig, ScanConfig
from hebra.errors import MemoryIdentityError
from hebra.indexing import index_repository
from hebra.memory import open_memory

SOURCE = (
    "def apply_tax(amount):\n"
    "    rate = 0.2\n"
    "    return amount * (1 + rate)\n"
    "\n"
    "def other():\n"
    "    return 1\n"
)


def git(root: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="bench",
        GIT_AUTHOR_EMAIL="bench@example.invalid",
        GIT_COMMITTER_NAME="bench",
        GIT_COMMITTER_EMAIL="bench@example.invalid",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)


def repo(base: Path, name: str) -> Path:
    root = base / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "tax.py").write_text(SOURCE)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "one")
    git(root, "commit", "-q", "--allow-empty", "-m", "two")
    return root


def reindex(root: Path) -> None:
    index_repository(root, config=IndexConfig(scan=ScanConfig()))


def _symlink(root: Path) -> None:
    target = root.parent / f"{root.name}-outside.py"
    target.write_text(SOURCE)
    (root / "tax.py").unlink()
    (root / "tax.py").symlink_to(target)


def _rename(root: Path) -> None:
    git(root, "mv", "tax.py", "levy.py")
    reindex(root)


def _branch(root: Path, same: bool) -> None:
    git(root, "checkout", "-q", "-b", "other", "HEAD~1")
    if not same:
        (root / "tax.py").write_text(SOURCE.replace("0.2", "0.3"))
        git(root, "commit", "-q", "-am", "change")


def _reset(root: Path, same: bool) -> None:
    git(root, "reset", "-q", "--hard", "HEAD~1")
    if not same:
        (root / "tax.py").write_text(SOURCE.replace("0.2", "0.3"))


# name, kind, references, mutation, expected record status, expected first-ref status,
# expected revision note fragment (or None)
Scenario = tuple[str, str, list[str], Callable[[Path], None], str, str | None, str | None]
SCENARIOS: list[Scenario] = [
    ("unchanged", "fact", ["tax.py:1-3"], lambda r: None, "current", "current", None),
    (
        "edit elsewhere",
        "fact",
        ["tax.py:1-3"],
        lambda r: (r / "tax.py").write_text(SOURCE.replace("return 1", "return 2")),
        "current",
        "current",
        None,
    ),
    (
        "edit referenced lines",
        "fact",
        ["tax.py:1-3"],
        lambda r: (r / "tax.py").write_text(SOURCE.replace("0.2", "0.25")),
        "stale",
        "changed",
        None,
    ),
    (
        "lines moved",
        "fact",
        ["tax.py:1-3"],
        lambda r: (r / "tax.py").write_text("import os\n\n" + SOURCE),
        "stale",
        "moved",
        None,
    ),
    (
        "file deleted",
        "fact",
        ["tax.py:1-3"],
        lambda r: (r / "tax.py").unlink(),
        "stale",
        "missing",
        None,
    ),
    ("file renamed", "fact", ["tax.py:1-3"], _rename, "stale", "renamed", None),
    ("replaced by symlink", "fact", ["tax.py:1-3"], _symlink, "stale", "replaced", None),
    (
        "branch, lines differ",
        "fact",
        ["tax.py:1-3"],
        lambda r: _branch(r, False),
        "stale",
        "changed",
        "not in HEAD's history",
    ),
    (
        "branch, lines same",
        "fact",
        ["tax.py:1-3"],
        lambda r: _branch(r, True),
        "current",
        "current",
        "not in HEAD's history",
    ),
    (
        "reset, lines same",
        "fact",
        ["tax.py:1-3"],
        lambda r: _reset(r, True),
        "current",
        "current",
        "not in HEAD's history",
    ),
    (
        "reset, lines differ",
        "fact",
        ["tax.py:1-3"],
        lambda r: _reset(r, False),
        "stale",
        "changed",
        "not in HEAD's history",
    ),
    ("decision", "decision", [], lambda r: None, "decision", None, None),
    (
        "decision, reference changed",
        "decision",
        ["tax.py:1-3"],
        lambda r: (r / "tax.py").write_text(SOURCE.replace("0.2", "0.4")),
        "decision",
        "changed",
        None,
    ),
    ("no references", "fact", [], lambda r: None, "unverifiable", None, None),
]


def run(out: Path) -> dict:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        for n, (name, kind, refs, mutate, want, want_ref, want_note) in enumerate(SCENARIOS):
            root = repo(base, f"r{n}")
            reindex(root)
            with open_memory(root, create=True) as memory:
                record_id = memory.add(kind, "apply tax uses a fixed rate", refs).record.id
            mutate(root)
            with open_memory(root, create=False) as memory:
                view = memory.view(memory.get(record_id))
            got_ref = view.checks[0].status if view.checks else None
            note_ok = want_note is None or want_note in view.revision_note
            results.append(
                {
                    "scenario": name,
                    "expected_status": want,
                    "status": view.status,
                    "expected_reference_status": want_ref,
                    "reference_status": got_ref,
                    "revision_note": view.revision_note,
                    "correct": view.status == want and got_ref == want_ref and note_ok,
                }
            )
        # Cross-repository collision: a store copied into an unrelated repository.
        first, second = repo(base, "x1"), base / "x2"
        second.mkdir()
        git(second, "init", "-q", "-b", "main")
        (second / "tax.py").write_text(SOURCE)
        git(second, "add", "-A")
        git(second, "commit", "-q", "-m", "unrelated")
        with open_memory(first, create=True) as memory:
            memory.add("fact", "apply tax", ["tax.py:1-3"])
        shutil.copytree(first / ".hebra", second / ".hebra")
        try:
            open_memory(second, create=False).__exit__(None, None, None)
            refused = False
        except MemoryIdentityError:
            refused = True
        results.append(
            {
                "scenario": "store from another repository",
                "expected_status": "refused",
                "status": "refused" if refused else "opened",
                "expected_reference_status": None,
                "reference_status": None,
                "revision_note": "",
                "correct": refused,
            }
        )
    correct = sum(r["correct"] for r in results)
    summary = {
        "schema": "hebra.bench.memory-freshness/v1",
        "scenarios": len(results),
        "correct": correct,
        "accuracy": round(correct / len(results), 4),
        "results": results,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    (out / "results.json").write_text(text)
    print(
        f"{correct}/{len(results)} scenarios correct; sha256 "
        f"{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    )
    for r in results:
        if not r["correct"]:
            print("  INCORRECT:", r)
    return summary


if __name__ == "__main__":
    run(Path(sys.argv[1]))
