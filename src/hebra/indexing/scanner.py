"""Safe discovery and reading of repository text files.

Repository content is treated strictly as data: files are read, hashed and
decoded, never imported or executed. Symlinks are never followed, special
files are never opened, and every skipped file is counted by reason.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from hebra.config import ScanConfig
from hebra.indexing.gitmeta import list_git_files

_BINARY_SNIFF_BYTES = 8192
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


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


def _walk_filesystem(root: Path, config: ScanConfig, stats: ScanStats) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            if name in config.excluded_dirs:
                continue
            if Path(dirpath, name).is_symlink():
                stats.skipped["symlink"] += 1
                continue
            kept.append(name)
        dirnames[:] = sorted(kept)
        rel_dir = Path(dirpath).relative_to(root)
        found.extend((rel_dir / name).as_posix() for name in filenames)
    return sorted(found)


def _candidate_paths(root: Path, config: ScanConfig, stats: ScanStats) -> list[str]:
    if config.use_git:
        git_paths = list_git_files(root)
        if git_paths is not None:
            stats.file_source = "git"
            return git_paths
    stats.file_source = "filesystem"
    return _walk_filesystem(root, config, stats)


def _read_regular_file(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(limit + 1)
    finally:
        os.close(fd)


def scan_repository(root: Path, config: ScanConfig, stats: ScanStats) -> Iterator[SourceFile]:
    """Yield readable UTF-8 text files under ``root`` in sorted path order.

    ``stats`` is updated as the iterator is consumed.
    """
    real_root = Path(os.path.realpath(root))
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

        full = root.joinpath(*rel.parts)
        try:
            info = full.lstat()
        except FileNotFoundError:
            stats.skipped["missing"] += 1
            continue
        except OSError:
            stats.skipped["unreadable"] += 1
            continue
        if stat.S_ISLNK(info.st_mode):
            stats.skipped["symlink"] += 1
            continue
        if not stat.S_ISREG(info.st_mode):
            stats.skipped["not-regular-file"] += 1
            continue
        # Reject paths that traverse a symlinked parent directory.
        if os.path.realpath(full) != str(real_root.joinpath(*rel.parts)):
            stats.skipped["symlink"] += 1
            continue
        if info.st_size > config.max_file_bytes:
            stats.skipped["too-large"] += 1
            continue
        if stats.accepted >= config.max_files:
            stats.skipped["file-limit"] += 1
            stats.truncated = True
            continue
        if total_bytes + info.st_size > config.max_total_bytes:
            stats.skipped["total-size-limit"] += 1
            stats.truncated = True
            continue

        try:
            data = _read_regular_file(full, config.max_file_bytes)
        except OSError:
            stats.skipped["unreadable"] += 1
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
            f"(max_files={config.max_files}, max_total_bytes={config.max_total_bytes})"
        )
