from __future__ import annotations

from pathlib import Path

from conftest import git, requires_git
from hebra.indexing.gitmeta import read_head_revision

SHA_A = "a" * 40
SHA_B = "b" * 40


def make_git_dir(root: Path, head: str) -> Path:
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text(head + "\n")
    return git_dir


def test_missing_git_metadata(tmp_path: Path) -> None:
    assert read_head_revision(tmp_path) is None


def test_loose_ref(tmp_path: Path) -> None:
    git_dir = make_git_dir(tmp_path, "ref: refs/heads/main")
    (git_dir / "refs" / "heads" / "main").write_text(SHA_A + "\n")
    assert read_head_revision(tmp_path) == SHA_A


def test_packed_ref(tmp_path: Path) -> None:
    git_dir = make_git_dir(tmp_path, "ref: refs/heads/main")
    (git_dir / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{SHA_B} refs/heads/main\n^{SHA_A}\n"
    )
    assert read_head_revision(tmp_path) == SHA_B


def test_detached_head(tmp_path: Path) -> None:
    make_git_dir(tmp_path, SHA_A)
    assert read_head_revision(tmp_path) == SHA_A


def test_unborn_branch(tmp_path: Path) -> None:
    make_git_dir(tmp_path, "ref: refs/heads/main")
    assert read_head_revision(tmp_path) is None


def test_malicious_ref_path_is_rejected(tmp_path: Path) -> None:
    make_git_dir(tmp_path, "ref: refs/../../../etc/passwd")
    assert read_head_revision(tmp_path) is None


def test_gitdir_pointer_file(tmp_path: Path) -> None:
    real = tmp_path / "real_git"
    (real / "refs" / "heads").mkdir(parents=True)
    (real / "HEAD").write_text("ref: refs/heads/dev\n")
    (real / "refs" / "heads" / "dev").write_text(SHA_A)
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text("gitdir: ../real_git\n")
    assert read_head_revision(work) == SHA_A


def test_subdirectory_finds_parent_repository(tmp_path: Path) -> None:
    git_dir = make_git_dir(tmp_path, "ref: refs/heads/main")
    (git_dir / "refs" / "heads" / "main").write_text(SHA_A)
    sub = tmp_path / "pkg" / "sub"
    sub.mkdir(parents=True)
    assert read_head_revision(sub) == SHA_A


@requires_git
def test_matches_git_rev_parse(make_git_repo) -> None:
    root = make_git_repo({"a.py": "x = 1\n"})
    assert read_head_revision(root) == git(root, "rev-parse", "HEAD")
    git(root, "pack-refs", "--all")
    assert read_head_revision(root) == git(root, "rev-parse", "HEAD")
