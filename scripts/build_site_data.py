"""Generate the website's static data from the real engine and recorded results.

Outputs (site/src/data/):

explorer.json    Real `pack_context` output for every (task, budget, method)
                 combination offered by the website, computed on the
                 synthetic example repository in examples/demo-shop.
memory.json      A real memory record followed through file changes.
benchmarks.json  Numbers copied from the validated benchmark summaries and
                 raw records in benchmarks/results (never re-derived by hand).

The example repository is copied into a temporary Git repository with fixed
author/committer dates, so commit ids and all outputs are deterministic. Local
paths are never written. CI re-runs this script and fails if the committed
data differs (`--check`).

Usage: python scripts/build_site_data.py [--check]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from hebra import __version__
from hebra.config import IndexConfig, ScanConfig
from hebra.context.packer import pack_context
from hebra.indexing import index_repository
from hebra.memory import open_memory

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "src" / "data"
REPO_URL = "https://github.com/AnvitDevadiga/hebra"

TASKS = [
    {"id": "discount", "text": "Discount codes should be applied before tax in the cart total"},
    {"id": "retry", "text": "Payment charges should retry transient gateway failures"},
    {"id": "tax", "text": "Add a tax rate for Canadian shipping regions"},
]
BUDGETS = [400, 700, 1200]
METHODS = {
    "baseline": {"retrieval": "bm25", "strategy": "greedy", "label": "BM25 + greedy (default)"},
    "optimized": {
        "retrieval": "bm25_graph",
        "strategy": "optimized",
        "label": "BM25 + graph + optimized (experimental)",
    },
}


def git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="demo",
        GIT_AUTHOR_EMAIL="demo@example.invalid",
        GIT_COMMITTER_NAME="demo",
        GIT_COMMITTER_EMAIL="demo@example.invalid",
        GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00",
        GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return subprocess.run(
        ["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def demo_repo(base: Path) -> Path:
    repo = base / "demo-shop"
    shutil.copytree(ROOT / "examples" / "demo-shop", repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "demo shop")
    return repo


def explorer(repo: Path) -> dict:
    index_repository(repo, config=IndexConfig(scan=ScanConfig()))
    runs = {}
    for task in TASKS:
        for budget in BUDGETS:
            for name, method in METHODS.items():
                r = pack_context(
                    repo,
                    task["text"],
                    budget,
                    retrieval=method["retrieval"],
                    strategy=method["strategy"],
                )
                data = r.to_dict()
                runs[f"{task['id']}|{budget}|{name}"] = {
                    "used": r.used_tokens,
                    "budget": budget,
                    "header_tokens": r.header_tokens,
                    "utility": data["method"]["utility"],
                    "selected": [
                        {
                            k: s[k]
                            for k in (
                                "rank",
                                "path",
                                "start_line",
                                "end_line",
                                "kind",
                                "symbol",
                                "tokens",
                                "reason",
                                "matched_terms",
                            )
                        }
                        | {
                            "sources": s["retrieval"]["sources"],
                            "evidence": s["retrieval"]["evidence"],
                        }
                        for s in data["selected"]
                    ],
                    "excluded": [
                        {
                            k: e[k]
                            for k in (
                                "rank",
                                "path",
                                "start_line",
                                "end_line",
                                "kind",
                                "symbol",
                                "reason",
                            )
                        }
                        for e in data["excluded"]
                    ],
                    "trace": data["trace"],
                    "context": r.markdown,
                }
    files = {
        str(p.relative_to(repo)): p.read_text()
        for p in sorted(repo.rglob("*.py"))
        if ".hebra" not in p.parts
    }
    return {
        "hebra_version": __version__,
        "tokenizer": "o200k_base",
        "repository": "examples/demo-shop (synthetic)",
        "tasks": TASKS,
        "budgets": BUDGETS,
        "methods": {k: v["label"] for k, v in METHODS.items()},
        "runs": runs,
        "files": files,
    }


def memory_demo(base: Path) -> dict:
    repo = demo_repo(base / "memory")
    index_repository(repo, config=IndexConfig(scan=ScanConfig()))
    steps = []

    def snapshot(title: str, explanation: str) -> None:
        with open_memory(repo, create=False) as memory:
            view = memory.views(include_invalidated=True)[0]
        data = view.to_dict()
        data["id"] = "mem_example"  # real ids are random; replaced for a stable page
        for key in ("created_at", "updated_at"):
            data.pop(key)
        steps.append({"title": title, "explanation": explanation, "record": data})

    with open_memory(repo, create=True) as memory:
        memory.add(
            "fact",
            "EU and UK orders are taxed at 20 percent in tax_for_region.",
            ["shop/tax.py:3-3"],
        )
    snapshot("Record added", "The referenced line was read and its SHA-256 stored.")
    (repo / "shop" / "tax.py").write_text(
        (repo / "shop" / "tax.py").read_text() + "\n\ndef supported_regions():\n"
        "    return sorted(TAX_RATES)\n"
    )
    snapshot(
        "Unrelated edit",
        "The file changed, but the referenced line is byte-identical, so the record stays current.",
    )
    text = (repo / "shop" / "tax.py").read_text().replace('"eu": 0.20', '"eu": 0.21')
    (repo / "shop" / "tax.py").write_text(text)
    git(repo, "commit", "-q", "-am", "raise EU rate")
    snapshot(
        "Referenced line changed",
        "The rate changed and was committed. The stored hash no longer matches, so the record "
        "is stale and is excluded from packed context.",
    )
    return {"steps": steps}


def benchmarks() -> dict:
    results = ROOT / "benchmarks" / "results"

    def load(path: str) -> dict:
        return json.loads((results / path).read_text())

    tasks = {
        t["task_id"]: t
        for t in map(json.loads, (ROOT / "benchmarks/tasks/v1.jsonl").read_text().splitlines())
    }
    records = [
        json.loads(line) for line in (results / "v2-heldout/records.jsonl").read_text().splitlines()
    ]
    misses = sorted(
        {r["task_id"] for r in records if r["kind"] == "context" and r["budget"] == 8000}
        - {
            r["task_id"]
            for r in records
            if r["kind"] == "context" and r["budget"] == 8000 and r["any_hit"]
        }
    )
    manifest = load("v2-heldout/manifest.json")
    return {
        "dataset": manifest["dataset"],
        "tasks_sha256": manifest["tasks_sha256"],
        "repositories": manifest["repositories"],
        "tokenizer": manifest["tokenizer"],
        "budgets": manifest["budgets"],
        "runs": {
            name: {
                "hebra_commit": load(f"{name}/manifest.json")["hebra_commit"],
                "split": load(f"{name}/manifest.json")["split"],
                "summary": load(f"{name}/summary.json"),
                "source": f"{REPO_URL}/tree/main/benchmarks/results/{name}",
            }
            for name in ("v1-heldout", "v2-heldout")
        },
        "memory_freshness": {
            k: v for k, v in load("v2-memory-freshness/results.json").items() if k != "finished_at"
        },
        "index_timing": {
            name: {k: r[k] for k in ("files_indexed", "seconds_median")}
            for name, r in load("v2-index-timing.json")["results"].items()
        },
        "failures_at_8000": {
            "count": len(misses),
            "of": len({r["task_id"] for r in records}),
            "examples": [
                {
                    "task_id": t,
                    "task": tasks[t]["task"],
                    "labels": [f"{lb['path']}::{lb['qualname']}" for lb in tasks[t]["labels"]],
                }
                for t in misses[:6]
            ],
        },
        "agent_experiments": "none run",
        "docs": f"{REPO_URL}/blob/main/docs/benchmarks.md",
    }


def build() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        outputs = {
            "explorer.json": explorer(demo_repo(base)),
            "memory.json": memory_demo(base),
            "benchmarks.json": benchmarks(),
        }
        text = {
            name: json.dumps(data, indent=2, sort_keys=True) + "\n"
            for name, data in outputs.items()
        }
        for name, content in text.items():
            if tmp in content or str(Path.home()) in content:
                raise SystemExit(f"{name} would leak a local path")
    return text


def main() -> int:
    text = build()
    if "--check" in sys.argv:
        stale = [n for n, c in text.items() if (OUT / n).read_text() != c]
        if stale:
            print(f"site data is out of date: {stale}; run scripts/build_site_data.py")
            return 1
        print("site data is up to date")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, content in text.items():
        (OUT / name).write_text(content)
    print(f"wrote {', '.join(text)} to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
