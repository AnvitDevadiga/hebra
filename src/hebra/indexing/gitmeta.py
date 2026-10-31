"""Read-only Git metadata.

The HEAD revision is read directly from files under ``.git`` so it works
without a ``git`` binary and never runs repository-configured programs.
File listing uses ``git ls-files`` because it is the only reliable way to
honour ``.gitignore``; it runs with a fixed argv, no shell, a sanitised
environment, and ``core.fsmonitor`` disabled. If a repository has Git
metadata but listing fails, Hebra stops rather than walking the filesystem,
which would expose ignored files.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from hebra.errors import GitError

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


def has_git_metadata(start: Path) -> bool:
    """True if ``start`` or a parent contains a ``.git`` entry, valid or not."""
    return any(os.path.lexists(candidate / ".git") for candidate in (start, *start.parents))


@dataclass(frozen=True)
class GitListing:
    paths: list[str]  # in Git's output order
    truncated: bool  # more than ``max_entries`` paths exist
    entries_read: int  # paths read from git's output, including one look-ahead


def list_git_files(
    repo_root: Path, *, include_untracked: bool = False, max_entries: int = 200_000
) -> GitListing:
    """List files Git knows about under ``repo_root``, relative to it.

    By default this is ``git ls-files --cached``: every path in Git's index.
    That includes committed files *and* newly staged files that have never
    been committed, and it includes tracked files even if they match
    ``.gitignore``. It excludes untracked files and ignored untracked files.
    Hebra then reads each path's current working-tree content, so unstaged
    edits are indexed and paths deleted from the working tree are skipped.

    With ``include_untracked``, untracked files not excluded by ``.gitignore``
    (and other standard exclude files) are added after the indexed paths.

    Output is streamed in Git's order and reading stops once ``max_entries``
    paths are collected; one further path is read only to report truncation.

    Raises :class:`GitError` if ``git`` is missing or the listing fails; callers
    must not fall back to an unfiltered filesystem walk in that case.
    """
    git = shutil.which("git")
    if git is None:
        raise GitError(
            f"{repo_root} contains Git metadata but the `git` executable was not found; install "
            "Git, or pass --no-git to walk the filesystem without .gitignore filtering"
        )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    argv = [git, "-c", "core.fsmonitor=false", "-c", "core.quotepath=off", "ls-files", "-z"]
    argv += ["--cached", "--others", "--exclude-standard"] if include_untracked else ["--cached"]

    seen: set[str] = set()
    paths: list[str] = []
    entries_read = 0
    truncated = False
    timed_out = threading.Event()
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                argv, cwd=repo_root, env=env, stdout=subprocess.PIPE, stderr=err
            )
        except OSError as exc:
            raise GitError(f"could not run git in {repo_root}: {exc}") from exc

        def on_timeout() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(_GIT_TIMEOUT_SECONDS, on_timeout)
        timer.start()
        try:
            assert proc.stdout is not None
            pending = b""
            while not truncated and (chunk := proc.stdout.read(65536)):
                *entries, pending = (pending + chunk).split(b"\0")
                for entry in entries:
                    if not entry:
                        continue
                    entries_read += 1
                    path = entry.decode("utf-8", errors="surrogateescape")
                    if path in seen:  # unmerged paths can be listed more than once
                        continue
                    if len(paths) == max_entries:
                        truncated = True
                        proc.kill()
                        break
                    seen.add(path)
                    paths.append(path)
            returncode = proc.wait()
        finally:
            timer.cancel()
            if proc.stdout is not None:
                proc.stdout.close()
        if timed_out.is_set():
            raise GitError(
                f"`git ls-files` in {repo_root} did not finish within {_GIT_TIMEOUT_SECONDS}s"
            )
        if not truncated and returncode != 0:
            err.seek(0)
            detail = err.read(2000).decode("utf-8", errors="replace").strip()
            raise GitError(
                f"`git ls-files` failed in {repo_root} (exit {returncode}): "
                f"{detail or 'no error output'}. Fix the repository (for example `git status` "
                "should succeed), or pass --no-git to walk the filesystem without .gitignore "
                "filtering"
            )
    return GitListing(paths, truncated, entries_read)


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def git_output(repo_root: Path, *args: str) -> str | None:
    """Stdout of a read-only git command, or None if git is missing or it fails.

    Same safety settings as :func:`list_git_files`: fixed argv, no shell,
    sanitised environment, ``core.fsmonitor`` disabled, bounded runtime.
    """
    git = shutil.which("git")
    if git is None or find_git_dir(repo_root) is None:
        return None
    try:
        result = subprocess.run(
            [git, "-c", "core.fsmonitor=false", *args],
            cwd=repo_root,
            env=_git_env(),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip()


def root_commits(repo_root: Path) -> list[str]:
    """Sorted parentless commits reachable from HEAD (the repository's roots)."""
    out = git_output(repo_root, "rev-list", "--max-parents=0", "HEAD")
    return sorted(line for line in (out or "").splitlines() if _HEX_SHA.match(line))


def is_ancestor(repo_root: Path, revision: str) -> bool | None:
    """Whether ``revision`` is in HEAD's history; None if it cannot be determined."""
    if not _HEX_SHA.match(revision):
        return None
    git = shutil.which("git")
    if git is None or find_git_dir(repo_root) is None:
        return None
    try:
        result = subprocess.run(
            [git, "-c", "core.fsmonitor=false", "merge-base", "--is-ancestor", revision, "HEAD"],
            cwd=repo_root,
            env=_git_env(),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # 0: ancestor, 1: not an ancestor, anything else (e.g. unknown commit): treat as not.
    return result.returncode == 0
