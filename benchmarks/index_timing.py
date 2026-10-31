"""Cold full-index time for each pinned repository head.

Each measurement indexes into a fresh temporary database (no reuse of any
earlier index), so it is a full index: scan, hash, parse, chunk, FTS insert,
and graph resolution. Three repetitions per repository; the median is
reported along with all samples. Timings are from one machine and vary.

Usage: python benchmarks/index_timing.py CONFIG CACHE_DIR OUT_JSON
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import git

from hebra.indexing import index_repository

REPETITIONS = 3


def main() -> int:
    config = json.loads(Path(sys.argv[1]).read_text())
    cache, out = Path(sys.argv[2]), Path(sys.argv[3])
    results = {}
    for spec in config["repositories"]:
        repo = cache / spec["name"]
        git(repo, "checkout", "-q", "--force", "--detach", spec["pinned_head"])
        git(repo, "clean", "-q", "-fd")
        samples = []
        report = None
        for _ in range(REPETITIONS):
            with tempfile.TemporaryDirectory() as tmp:
                started = time.perf_counter()
                report = index_repository(repo, Path(tmp) / "index.sqlite3")
                samples.append(round(time.perf_counter() - started, 4))
        assert report is not None
        results[spec["name"]] = {
            "commit": spec["pinned_head"],
            "files_indexed": report.files_indexed,
            "chunks": report.chunks_written,
            "graph_edges": report.graph_edges,
            "seconds_samples": samples,
            "seconds_median": statistics.median(samples),
        }
    data = {
        "schema": "hebra.bench.index-timing/v1",
        "hebra_commit": git(Path(__file__).resolve().parent.parent, "rev-parse", "HEAD"),
        "platform": {
            "python": platform.python_version(),
            "system": platform.platform(),
            "machine": platform.machine(),
        },
        "repetitions": REPETITIONS,
        "results": results,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    for name, r in results.items():
        median, samples = r["seconds_median"], r["seconds_samples"]
        print(f"{name}: {r['files_indexed']} files, median {median} s {samples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
