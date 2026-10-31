from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import git, requires_git
from hebra.config import ScanConfig
from hebra.indexing.scanner import ScanStats, SourceFile, scan_repository


def scan(root: Path, **config: object) -> tuple[list[SourceFile], ScanStats]:
    stats = ScanStats()
    cfg = ScanConfig(**{"use_git": False, **config})  # type: ignore[arg-type]
    return list(scan_repository(root, cfg, stats)), stats


def paths(files: list[SourceFile]) -> list[str]:
    return [f.path for f in files]


def test_empty_repository(make_repo) -> None:
    files, stats = scan(make_repo({}))
    assert files == []
    assert stats.candidates == 0 and stats.accepted == 0


def test_default_exclusions(make_repo) -> None:
    root = make_repo(
        {
            "src/app.py": "x = 1\n",
            "node_modules/pkg/index.js": "x\n",
            ".venv/lib/site.py": "x\n",
            "build/out.py": "x\n",
            "pkg/__pycache__/mod.py": "x\n",
            "package-lock.json": "{}\n",
            "static/app.min.js": "x\n",
            "logo.png": b"\x89PNG\r\n",
        }
    )
    files, stats = scan(root)
    assert paths(files) == ["src/app.py"]
    assert stats.skipped["excluded"] == 3  # lock file, .min.js, .png (dirs are pruned)


def test_large_files_are_skipped(make_repo) -> None:
    root = make_repo({"big.py": "x = 1\n" * 1000, "small.py": "y = 2\n"})
    files, stats = scan(root, max_file_bytes=1000)
    assert paths(files) == ["small.py"]
    assert stats.skipped["too-large"] == 1


def test_binary_files_are_skipped(make_repo) -> None:
    root = make_repo({"data.bin.txt": b"abc\x00def", "ok.txt": "fine\n"})
    files, stats = scan(root)
    assert paths(files) == ["ok.txt"]
    assert stats.skipped["binary"] == 1


def test_invalid_utf8_is_skipped_not_crashing(make_repo) -> None:
    root = make_repo({"latin1.py": "name = 'caf\xe9'\n".encode("latin-1"), "ok.py": "a = 1\n"})
    files, stats = scan(root)
    assert paths(files) == ["ok.py"]
    assert stats.skipped["invalid-encoding"] == 1


def test_unicode_content_bom_and_newlines(make_repo) -> None:
    root = make_repo(
        {
            "uni.py": "名前 = '日本語'\r\nemoji = '🚀'\r\n",
            "bom.py": b"\xef\xbb\xbfx = 1\n",
            "cr.txt": "a\rb\r",
        }
    )
    files, _ = scan(root)
    by_path = {f.path: f for f in files}
    assert by_path["uni.py"].text == "名前 = '日本語'\nemoji = '🚀'\n"
    assert by_path["bom.py"].text == "x = 1\n"
    assert by_path["cr.txt"].text == "a\nb\n"


def test_content_hash_is_of_raw_bytes(make_repo) -> None:
    import hashlib

    root = make_repo({"a.py": "x = 1\r\n"})
    files, _ = scan(root)
    assert files[0].sha256 == hashlib.sha256(b"x = 1\r\n").hexdigest()


def test_symlinks_are_never_followed(make_repo, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n")
    root = make_repo({"real.py": "x = 1\n"})
    os.symlink(outside / "secret.py", root / "link_out.py")
    os.symlink(root / "real.py", root / "link_in.py")
    os.symlink(outside, root / "linked_dir")
    files, stats = scan(root)
    assert paths(files) == ["real.py"]
    assert stats.skipped["symlink"] == 3


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_special_files_are_not_opened(make_repo) -> None:
    root = make_repo({"ok.py": "x = 1\n"})
    os.mkfifo(root / "pipe.py")
    files, stats = scan(root)
    assert paths(files) == ["ok.py"]
    assert stats.skipped["not-regular-file"] == 1


def test_duplicate_filenames_in_different_directories(make_repo) -> None:
    root = make_repo({"a/utils.py": "A = 1\n", "b/utils.py": "B = 2\n", "utils.py": "C = 3\n"})
    files, _ = scan(root)
    # Walk order: a directory's files first, then its subdirectories by name.
    assert paths(files) == ["utils.py", "a/utils.py", "b/utils.py"]


def test_file_count_limit_truncates_deterministically(make_repo) -> None:
    root = make_repo({f"m{i:02d}.py": f"v = {i}\n" for i in range(10)})
    files, stats = scan(root, max_files=4)
    assert paths(files) == ["m00.py", "m01.py", "m02.py", "m03.py"]
    assert stats.truncated
    assert stats.skipped["file-limit"] == 6
    assert stats.warnings


def test_total_size_limit(make_repo) -> None:
    root = make_repo({f"m{i}.py": "x" * 100 + "\n" for i in range(5)})
    files, stats = scan(root, max_total_bytes=250)
    assert len(files) == 2
    assert stats.skipped["total-size-limit"] == 3


@requires_git
def test_git_mode_indexes_tracked_files_only_by_default(make_git_repo) -> None:
    root = make_git_repo({".gitignore": "ignored/\n*.log\n", "tracked.py": "x = 1\n"})
    (root / "ignored").mkdir()
    (root / "ignored" / "skip.py").write_text("x = 1\n")
    (root / "debug.log").write_text("noise\n")
    (root / "new_untracked.py").write_text("y = 2\n")
    files, stats = scan(root, use_git=True)
    assert stats.file_source == "git"
    assert paths(files) == [".gitignore", "tracked.py"]

    # Opt-in adds untracked files, but .gitignore is still honoured.
    files, _ = scan(root, use_git=True, include_untracked=True)
    assert sorted(paths(files)) == [".gitignore", "new_untracked.py", "tracked.py"]


@requires_git
def test_git_mode_tracked_file_deleted_from_worktree(make_git_repo) -> None:
    root = make_git_repo({"keep.py": "x = 1\n", "gone.py": "y = 2\n"})
    (root / "gone.py").unlink()
    files, stats = scan(root, use_git=True)
    assert paths(files) == ["keep.py"]
    assert stats.skipped["missing"] == 1


@requires_git
def test_git_mode_skips_tracked_symlink(make_git_repo, tmp_path: Path) -> None:
    root = make_git_repo({"real.py": "x = 1\n"})
    os.symlink("/etc/hosts", root / "hosts.txt")
    git(root, "add", "hosts.txt")
    files, stats = scan(root, use_git=True)
    assert paths(files) == ["real.py"]
    assert stats.skipped["symlink"] == 1


def test_l2_filesystem_enumeration_is_bounded(make_repo) -> None:
    root = make_repo({f"d{i}/f{j}.py": "x = 1\n" for i in range(10) for j in range(10)})
    files, stats = scan(root, max_scan_entries=25)
    assert stats.truncated
    assert stats.candidates < 40  # stopped early instead of listing all 110 entries
    assert any("max_scan_entries" in w for w in stats.warnings)
    again, _ = scan(root, max_scan_entries=25)
    assert paths(again) == paths(files)  # deterministic truncation


@requires_git
def test_l2_git_listing_is_bounded(make_git_repo) -> None:
    root = make_git_repo({f"f{i:02d}.py": "x = 1\n" for i in range(30)})
    files, stats = scan(root, use_git=True, max_scan_entries=5)
    assert stats.truncated
    assert stats.candidates == 5
    assert paths(files) == [f"f{i:02d}.py" for i in range(5)]


class _CountingScandir:
    """Wraps os.scandir to count the entries the OS actually returns."""

    def __init__(self) -> None:
        self.entries = 0
        self.real = os.scandir

    def __call__(self, path):  # type: ignore[no-untyped-def]
        inner = self.real(path)
        outer = self

        class Wrapped:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *exc):  # type: ignore[no-untyped-def]
                inner.close()

            def __iter__(self):  # type: ignore[no-untyped-def]
                return self

            def __next__(self):  # type: ignore[no-untyped-def]
                entry = next(inner)
                outer.entries += 1
                return entry

        return Wrapped()


@pytest.fixture
def os_entries(monkeypatch) -> _CountingScandir:
    counter = _CountingScandir()
    monkeypatch.setattr(os, "scandir", counter)
    return counter


def test_walk_one_large_directory_is_bounded(make_repo, os_entries) -> None:
    root = make_repo({f"f{i:05d}.txt": "x\n" for i in range(3000)})
    os_entries.entries = 0
    files, stats = scan(root, max_scan_entries=40)
    assert os_entries.entries == 41  # 40 counted + 1 look-ahead, not 3000
    assert stats.entries_enumerated == os_entries.entries
    assert len(files) == stats.candidates == 40
    assert stats.truncated


def test_walk_exact_fit_is_not_truncated(make_repo, os_entries) -> None:
    root = make_repo({f"f{i}.txt": "x\n" for i in range(10)})
    os_entries.entries = 0
    files, stats = scan(root, max_scan_entries=10)
    assert len(files) == 10 and not stats.truncated
    assert os_entries.entries == 10


def test_walk_deeply_nested_directories(make_repo, os_entries) -> None:
    rel = "/".join(f"d{i}" for i in range(200))
    root = make_repo({f"{rel}/leaf.py": "x = 1\n", "top.py": "y = 2\n"})
    files, stats = scan(root)  # iterative: no recursion limit
    assert paths(files) == ["top.py", f"{rel}/leaf.py"]
    assert not stats.truncated
    os_entries.entries = 0
    files, stats = scan(root, max_scan_entries=50)
    assert stats.truncated and paths(files) == ["top.py"]
    assert os_entries.entries <= 51


def test_walk_order_contract(make_repo) -> None:
    root = make_repo(
        {rel: "x\n" for rel in ["b.txt", "a/z.txt", "a/b/c.txt", "a_file.txt", "c/a.txt"]}
    )
    files, _ = scan(root)
    # Pre-order; in each directory: files by name, then subdirectories by name.
    assert paths(files) == ["a_file.txt", "b.txt", "a/z.txt", "a/b/c.txt", "c/a.txt"]


def test_walk_never_enumerates_excluded_or_symlinked_directories(
    make_repo, tmp_path: Path, os_entries
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    for i in range(300):
        (outside / f"o{i}.py").write_text("x\n")
    files_map: dict[str, str | bytes] = {f"node_modules/p{i}.js": "x\n" for i in range(300)}
    files_map["app.py"] = "x = 1\n"
    root = make_repo(files_map)
    os.symlink(outside, root / "linked")
    os_entries.entries = 0
    files, stats = scan(root, max_scan_entries=10)
    assert os_entries.entries == 3  # app.py, linked, node_modules: contents never read
    assert paths(files) == ["app.py"]
    assert stats.skipped["symlink"] == 1 and not stats.truncated


def test_walk_truncation_is_repeatable_on_unchanged_tree(make_repo) -> None:
    root = make_repo({f"d{i % 7}/f{i:04d}.py": "x\n" for i in range(700)})
    first, _ = scan(root, max_scan_entries=123)
    for _ in range(3):
        again, stats = scan(root, max_scan_entries=123)
        assert paths(again) == paths(first) and stats.truncated


@requires_git
def test_git_listing_reports_enumeration(make_git_repo) -> None:
    root = make_git_repo({f"f{i:02d}.py": "x = 1\n" for i in range(30)})
    _, stats = scan(root, use_git=True, max_scan_entries=5)
    assert stats.entries_enumerated == 6  # 5 kept + 1 look-ahead
    _, stats = scan(root, use_git=True, max_scan_entries=30)
    assert stats.entries_enumerated == 30 and not stats.truncated
