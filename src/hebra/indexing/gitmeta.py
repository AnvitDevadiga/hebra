"""Read-only Git metadata.

The HEAD revision is read directly from files under ``.git`` so it works
without a ``git`` binary and never runs repository-configured programs.
File listing uses ``git ls-files`` (when available) because it is the only
reliable way to honour ``.gitignore``; it runs with a fixed argv, no shell,
a sanitised environment, and ``core.fsmonitor`` disabled.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

_HEX_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_SAFE_REF = re.compile(r"^refs/[A-Za-z0-9._/-]+$")
_GIT_TIMEOUT_SECONDS = 60


def find_git_dir(start: Path) -> Path | None:
    """Return the ``.git`` directory for ``start`` or any parent, if present."""
    for candidate in (start, *start.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            # Worktrees and submodules use a "gitdir: <path>" pointer file.
            try:
                text = dot_git.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                return None
            if not text.startswith("gitdir:"):
                return None
            target = Path(text.removeprefix("gitdir:").strip())
            if not target.is_absolute():
                target = candidate / target
            return target if target.is_dir() else None
    return None


def _read_ref(git_dir: Path, ref: str) -> str | None:
    if not _SAFE_REF.match(ref) or ".." in ref:
        return None
    common_dir = git_dir
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        try:
            common = Path(commondir_file.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError):
            return None
        common_dir = common if common.is_absolute() else git_dir / common

    for base in (git_dir, common_dir):
        loose = base / ref
        if loose.is_file():
            try:
                value = loose.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                return None
            return value if _HEX_SHA.match(value) else None

    packed = common_dir / "packed-refs"
    if packed.is_file():
        try:
            lines = packed.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return None
        for line in lines:
            if line.startswith(("#", "^")):
                continue
            sha, _, name = line.partition(" ")
            if name == ref and _HEX_SHA.match(sha):
                return sha
    return None


def read_head_revision(repo_root: Path) -> str | None:
    """Return the commit SHA that HEAD points to, or ``None`` if unavailable.

    Returns ``None`` for non-Git directories, unborn branches (no commits),
    and unreadable metadata.
    """
    git_dir = find_git_dir(repo_root)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if head.startswith("ref:"):
        return _read_ref(git_dir, head.removeprefix("ref:").strip())
    return head if _HEX_SHA.match(head) else None


def list_git_files(repo_root: Path) -> list[str] | None:
    """List tracked plus untracked-but-not-ignored files, relative to ``repo_root``.

    Returns ``None`` when ``git`` is unavailable or the command fails, so the
    caller can fall back to a filesystem walk.
    """
    git = shutil.which("git")
    if git is None or find_git_dir(repo_root) is None:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    argv = [
        git,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotepath=off",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    try:
        result = subprocess.run(
            argv,
            cwd=repo_root,
            env=env,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.decode("utf-8", errors="surrogateescape")
    # --cached and --others can both report a path during merges; dedupe.
    return sorted({p for p in raw.split("\0") if p})
