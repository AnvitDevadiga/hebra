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
    assert paths(files) == ["a/utils.py", "b/utils.py", "utils.py"]


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
    assert paths(files) == [".gitignore", "new_untracked.py", "tracked.py"]


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
