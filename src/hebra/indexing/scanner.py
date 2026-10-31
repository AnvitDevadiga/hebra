"""Safe discovery and reading of repository text files.

Repository content is treated strictly as data: files are read, hashed and
decoded, never imported or executed. Symlinks are never followed, special
files are never opened, and every skipped file is counted by reason.

File discovery policy:

* Git repositories: ``git ls-files`` (tracked files; untracked-but-not-ignored
  files only with ``include_untracked``). If Git metadata exists but listing
  fails, scanning stops with :class:`~hebra.errors.GitError`.
  Paths are taken in Git's index order. What ``--cached`` means precisely
  is documented in :func:`hebra.indexing.gitmeta.list_git_files`.
* ``use_git=False`` or no Git metadata: a bounded filesystem walk with the
  default exclusions (order contract below). ``.gitignore`` is not applied.
* Always: files matching sensitive-name patterns, and files containing a
  PEM private-key header, are skipped.

Filesystem walk contract:

* Pre-order depth-first. In each directory, entries are sorted by name
  (Python string order); files come first, then each subdirectory is walked
  completely before the next.
* ``max_scan_entries`` bounds the directory entries read from the OS, counting
  every entry (files, directories, symlinks, and excluded names alike). After
  the budget is spent, one more entry is requested only to decide whether the
  walk was truncated, so at most ``max_scan_entries + 1`` entries are read.
* Memory is bounded by the same limit: a directory's entries are buffered
  (and sorted) only up to the remaining budget, and pending subdirectories
  are themselves counted entries.
* Determinism: an unchanged tree that fits the budget always yields the same
  order. If the budget runs out *inside* a directory, which of its entries
  were read depends on the order the OS returns them (``os.scandir``), not on
  their names; that subset is stable for an unchanged directory on common
  filesystems but is not guaranteed by POSIX. A tree that changes during the
  walk can yield any mixture of old and new entries.
* Symlinked directories are never entered; directories are opened with
  ``O_NOFOLLOW`` and checked against the inode seen during listing.
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
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_SCANDIR_FD = os.scandir in os.supports_fd and bool(_O_NOFOLLOW and _O_DIRECTORY)

# (st_dev, st_ino, st_size, st_mtime_ns) of a file as it was opened for reading.
FileSignature = tuple[int, int, int, int]


@dataclass(frozen=True)
class SourceFile:
    path: str  # POSIX-style path relative to the repository root
    text: str  # decoded UTF-8 with newlines normalised to "\n"
    sha256: str  # hash of the raw bytes on disk
    size_bytes: int
    signature: FileSignature  # fstat of the opened file, taken before reading


@dataclass
class ScanStats:
    file_source: str = "filesystem"
    candidates: int = 0
    entries_enumerated: int = 0  # directory entries (or git paths) read from the OS
    candidate_paths: list[str] = field(default_factory=list)
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


def _open_directory(root: Path, rel_dir: str, expected_inode: int | None) -> int | None:
    """Open ``root/rel_dir`` without following a symlink at the final component.

    Returns ``None`` if the directory was replaced since it was listed.
    """
    path = root / rel_dir if rel_dir else root
    fd = os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    if expected_inode is not None and os.fstat(fd).st_ino != expected_inode:
        os.close(fd)
        return None
    return fd


def _walk_filesystem(root: Path, config: ScanConfig, stats: ScanStats) -> list[str]:
    """Bounded pre-order walk; see the module docstring for the exact contract."""
    found: list[str] = []
    remaining = config.max_scan_entries
    # Pending directories as (relative path, inode seen when listed), popped LIFO.
    stack: list[tuple[str, int | None]] = [("", None)]
    while stack:
        rel_dir, inode = stack.pop()
        files: list[str] = []
        subdirs: list[tuple[str, int]] = []
        fd: int | None = None
        try:
            if _SCANDIR_FD:
                fd = _open_directory(root, rel_dir, inode)
                if fd is None:
                    stats.skipped["replaced-directory"] += 1
                    continue
                listing = os.scandir(fd)
            else:  # pragma: no cover - platforms without scandir(fd)
                listing = os.scandir(root / rel_dir if rel_dir else root)
            with listing:
                for entry in listing:
                    stats.entries_enumerated += 1
                    if remaining == 0:
                        stats.truncated = True  # the look-ahead found one more entry
                        break
                    remaining -= 1
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_dir:
                        if entry.name not in config.excluded_dirs:
                            subdirs.append((entry.name, entry.inode()))
                    else:
                        # Files, symlinks (to anything), and special files are
                        # classified later by read_file_snapshot, which never
                        # follows links.
                        files.append(entry.name)
        except FileNotFoundError:
            stats.skipped["missing-directory"] += 1
            continue
        except OSError:
            stats.skipped["unreadable-directory"] += 1
            continue
        finally:
            if fd is not None:
                os.close(fd)
        prefix = f"{rel_dir}/" if rel_dir else ""
        found.extend(prefix + name for name in sorted(files))
        stack.extend((prefix + name, ino) for name, ino in sorted(subdirs, reverse=True))
        if stats.truncated:
            break
        if remaining == 0 and stack:
            stats.truncated = True  # unvisited directories remain
            break
    if stats.truncated:
        stats.warnings.append(
            f"stopped enumerating after max_scan_entries={config.max_scan_entries} entries"
        )
    return found


def _candidate_paths(
    root: Path, config: ScanConfig, stats: ScanStats, ignore: frozenset[str]
) -> list[str]:
    paths = _discover(root, config, stats)
    # Never treat Hebra's own index files (or their SQLite journals) as sources.
    return [p for p in paths if p not in ignore] if ignore else paths


def _discover(root: Path, config: ScanConfig, stats: ScanStats) -> list[str]:
    in_git = has_git_metadata(root)
    if in_git and config.use_git:
        listing = list_git_files(
            root, include_untracked=config.include_untracked, max_entries=config.max_scan_entries
        )
        stats.file_source = "git"
        stats.entries_enumerated += listing.entries_read
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


def list_candidates(
    root: Path, config: ScanConfig, ignore: frozenset[str] = frozenset()
) -> list[str]:
    """The candidate path list a scan would start from (used to detect changes)."""
    return _candidate_paths(Path(os.path.realpath(root)), config, ScanStats(), ignore)


@dataclass(frozen=True)
class FileSnapshot:
    data: bytes  # at most ``limit + 1`` bytes
    signature: FileSignature


def _signature(info: os.stat_result) -> FileSignature:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def read_file_snapshot(root: Path, rel: PurePosixPath, limit: int) -> FileSnapshot | str:
    """Read at most ``limit + 1`` bytes of ``root/rel`` without following symlinks.

    Returns the bytes with the file's signature, or a skip reason: "missing",
    "symlink", "not-regular-file", or "unreadable". ``root`` must already be
    resolved.
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
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return "not-regular-file"
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return FileSnapshot(handle.read(limit + 1), _signature(opened))
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)


def read_repository_file(root: Path, rel: PurePosixPath, limit: int) -> bytes | str:
    """Like :func:`read_file_snapshot`, returning only the bytes."""
    result = read_file_snapshot(root, rel, limit)
    return result if isinstance(result, str) else result.data


def changed_since_read(root: Path, path: str, signature: FileSignature) -> bool:
    """True if ``path`` no longer looks like the file that was read.

    Compares device, inode, size, and modification time without reading
    content; a same-size write within one timestamp tick can go unnoticed.
    """
    try:
        info = root.joinpath(*PurePosixPath(path).parts).lstat()
    except OSError:
        return True
    return not stat.S_ISREG(info.st_mode) or _signature(info) != signature


def scan_repository(
    root: Path, config: ScanConfig, stats: ScanStats, ignore: frozenset[str] = frozenset()
) -> Iterator[SourceFile]:
    """Yield readable UTF-8 text files under ``root`` in candidate order.

    Candidate order is Git's index order, or the walk order described in the
    module docstring.

    ``stats`` is updated as the iterator is consumed. Relative paths in
    ``ignore`` are dropped from the candidates.
    """
    root = Path(os.path.realpath(root))
    total_bytes = 0

    stats.candidate_paths = _candidate_paths(root, config, stats, ignore)
    for raw_path in stats.candidate_paths:
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

        snapshot = read_file_snapshot(root, rel, config.max_file_bytes)
        if isinstance(snapshot, str):
            stats.skipped[snapshot] += 1
            continue
        data = snapshot.data
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
            signature=snapshot.signature,
        )

    if stats.truncated:
        stats.warnings.append(
            "scan limits reached; some files were not indexed "
            f"(max_files={config.max_files}, max_total_bytes={config.max_total_bytes}, "
            f"max_scan_entries={config.max_scan_entries})"
        )
