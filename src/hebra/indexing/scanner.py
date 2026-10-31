"""Safe discovery and reading of repository text files.

Repository content is treated strictly as data: files are read, hashed and
decoded, never imported or executed. Symlinks are never followed, special
files are never opened, and every skipped file is counted by reason.

File discovery policy:

* Git repositories: ``git ls-files`` (tracked files; untracked-but-not-ignored
  files only with ``include_untracked``). If Git metadata exists but listing
  fails, scanning stops with :class:`~hebra.errors.GitError`.
* ``use_git=False`` or no Git metadata: a sorted, bounded filesystem walk with
  the default exclusions. ``.gitignore`` is not applied.
* Always: files matching sensitive-name patterns, and files containing a
  PEM private-key header, are skipped.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from hebra.config import CODE_SUFFIXES, ScanConfig
from hebra.indexing.gitmeta import has_git_metadata, list_git_files

_BINARY_SNIFF_BYTES = 8192
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


@dataclass(frozen=True)
class SourceFile:
    path: str  # POSIX-style path relative to the repository root
    text: str  # decoded UTF-8 with newlines normalised to "\n"
    sha256: str  # hash of the raw bytes on disk
    size_bytes: int


@dataclass
class ScanStats:
    file_source: str = "filesystem"
    candidates: int = 0
    accepted: int = 0
    bytes_read: int = 0
    truncated: bool = False
    skipped: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)


def _is_excluded(rel: PurePosixPath, config: ScanConfig) -> bool:
    if any(part in config.excluded_dirs for part in rel.parts[:-1]):
        return True
    name = rel.name
    if name in config.excluded_files:
        return True
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in config.excluded_suffixes)


def is_sensitive_name(name: str, config: ScanConfig) -> bool:
    lower = name.lower()
    for pattern in config.sensitive_patterns:
        if fnmatch.fnmatchcase(lower, pattern):
            if pattern == "credentials*" and PurePosixPath(lower).suffix in CODE_SUFFIXES:
                continue
            return True
    return False


def _walk_filesystem(root: Path, config: ScanConfig, stats: ScanStats) -> list[str]:
    """Depth-first walk in sorted order, stopping after ``max_scan_entries`` entries."""
    found: list[str] = []
    budget = config.max_scan_entries
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in sorted(dirnames):
            if name in config.excluded_dirs:
                continue
            if Path(dirpath, name).is_symlink():
                stats.skipped["symlink"] += 1
                continue
            kept.append(name)
        dirnames[:] = kept
        budget -= len(kept) + len(filenames)
        rel_dir = Path(dirpath).relative_to(root)
        found.extend((rel_dir / name).as_posix() for name in sorted(filenames))
        if budget <= 0:
            dirnames[:] = []
            stats.truncated = True
            stats.warnings.append(
                f"stopped enumerating after max_scan_entries={config.max_scan_entries} entries"
            )
            break
    return sorted(found)


def _candidate_paths(root: Path, config: ScanConfig, stats: ScanStats) -> list[str]:
    in_git = has_git_metadata(root)
    if in_git and config.use_git:
        listing = list_git_files(
            root, include_untracked=config.include_untracked, max_entries=config.max_scan_entries
        )
        stats.file_source = "git"
        if listing.truncated:
            stats.truncated = True
            stats.warnings.append(
                f"stopped reading `git ls-files` after max_scan_entries={config.max_scan_entries}"
            )
        return listing.paths
    if in_git:
        stats.warnings.append(
            "Git metadata found but --no-git was given: walking the filesystem, so .gitignore "
            "rules are NOT applied (sensitive-name filters still are)"
        )
    stats.file_source = "filesystem"
    return _walk_filesystem(root, config, stats)


def read_repository_file(root: Path, rel: PurePosixPath, limit: int) -> bytes | str:
    """Read at most ``limit + 1`` bytes of ``root/rel`` without following symlinks.

    Returns the bytes, or a skip reason: "missing", "symlink",
    "not-regular-file", or "unreadable". ``root`` must already be resolved.
    """
    full = root.joinpath(*rel.parts)
    try:
        info = full.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat.S_ISREG(info.st_mode):
        return "not-regular-file"
    # Reject paths that traverse a symlinked parent directory.
    if os.path.realpath(full) != str(root.joinpath(*rel.parts)):
        return "symlink"
    try:
        fd = os.open(full, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "symlink" if full.is_symlink() else "unreadable"
    try:
        # Re-check the opened object: the path may have been swapped after lstat.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "not-regular-file"
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(limit + 1)
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)


def scan_repository(root: Path, config: ScanConfig, stats: ScanStats) -> Iterator[SourceFile]:
    """Yield readable UTF-8 text files under ``root`` in sorted path order.

    ``stats`` is updated as the iterator is consumed.
    """
    root = Path(os.path.realpath(root))
    total_bytes = 0

    for raw_path in _candidate_paths(root, config, stats):
        stats.candidates += 1
        try:
            raw_path.encode("utf-8")
        except UnicodeEncodeError:
            stats.skipped["undecodable-path"] += 1
            continue
        rel = PurePosixPath(raw_path)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            stats.skipped["unsafe-path"] += 1
            continue
        if _is_excluded(rel, config):
            stats.skipped["excluded"] += 1
            continue
        if is_sensitive_name(rel.name, config):
            stats.skipped["sensitive-name"] += 1
            continue

        full = root.joinpath(*rel.parts)
        try:
            size = full.lstat().st_size
        except OSError:
            size = 0  # read_repository_file reports the precise reason
        if size > config.max_file_bytes:
            stats.skipped["too-large"] += 1
            continue
        if stats.accepted >= config.max_files:
            stats.skipped["file-limit"] += 1
            stats.truncated = True
            continue
        if total_bytes + size > config.max_total_bytes:
            stats.skipped["total-size-limit"] += 1
            stats.truncated = True
            continue

        data = read_repository_file(root, rel, config.max_file_bytes)
        if isinstance(data, str):
            stats.skipped[data] += 1
            continue
        if len(data) > config.max_file_bytes:  # grew after lstat
            stats.skipped["too-large"] += 1
            continue
        if b"\0" in data[:_BINARY_SNIFF_BYTES]:
            stats.skipped["binary"] += 1
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            stats.skipped["invalid-encoding"] += 1
            continue
        if _PRIVATE_KEY.search(text):
            stats.skipped["private-key-content"] += 1
            continue

        total_bytes += len(data)
        stats.bytes_read += len(data)
        stats.accepted += 1
        yield SourceFile(
            path=rel.as_posix(),
            text=text.replace("\r\n", "\n").replace("\r", "\n"),
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )

    if stats.truncated:
        stats.warnings.append(
            "scan limits reached; some files were not indexed "
            f"(max_files={config.max_files}, max_total_bytes={config.max_total_bytes}, "
            f"max_scan_entries={config.max_scan_entries})"
        )
