"""Repository and index path resolution, and the index-path safety policy.

Repositories are untrusted input, and the default index lives *inside* the
repository (``<repo>/.hebra/index.sqlite3``). A repository can therefore ship
a ``.hebra`` entry, or files inside it, that try to redirect Hebra's writes.

Policy for the default, repository-local index:

* ``.hebra`` must be a real directory, opened relative to the repository root
  with ``O_NOFOLLOW``; a symlink or any other file type is rejected.
* The ``.gitignore`` marker is created with ``O_CREAT | O_EXCL | O_NOFOLLOW``;
  an existing marker must be a regular file and is never rewritten.
* The database file is created or opened relative to the ``.hebra`` directory
  descriptor with ``O_NOFOLLOW``, and must be a regular file.
* SQLite sidecars (``-journal``, ``-wal``, ``-shm``) must be absent or regular
  files. (SQLite itself opens its journal with ``O_NOFOLLOW``.)
* After SQLite opens the database by path, the directory and file identities
  (device, inode) are re-checked so a swap between validation and open is
  detected before any write.

Policy for an explicit ``--db`` path: the caller chose the location, so its
parent directories are created as given and are *not* required to lie inside
the repository. The final path component must still not be a symlink or a
non-regular file, sidecars are checked the same way, and no marker file is
written.

For both, :mod:`hebra.indexing.store` refuses to modify an existing SQLite
database that is not a Hebra index.

Limits: these checks fully handle hostile repository *content* (static
symlinks and special files). They narrow but do not eliminate races with a
concurrent local attacker who can rewrite ``.hebra`` while Hebra runs: SQLite
opens the database by path, so a swap after the final identity check cannot
be prevented from user space. Platforms without ``dir_fd``/``O_NOFOLLOW``
support (Windows) get ``lstat``-based checks only.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from hebra.errors import IndexPathError, RepositoryError

INDEX_DIR_NAME = ".hebra"
INDEX_FILE_NAME = "index.sqlite3"
MARKER_NAME = ".gitignore"
SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_HAVE_DIR_FD = os.open in os.supports_dir_fd and bool(_O_NOFOLLOW and _O_DIRECTORY)

FileIdentity = tuple[int, int]  # (st_dev, st_ino)


@dataclass(frozen=True)
class IndexLocation:
    path: Path  # absolute path of the database file
    repo_local: bool  # True for the default <repo>/.hebra/index.sqlite3


def resolve_repository(path: Path) -> Path:
    if not path.exists():
        raise RepositoryError(f"repository path does not exist: {path}")
    if not path.is_dir():
        raise RepositoryError(f"repository path is not a directory: {path}")
    return path.resolve()


def index_location(repo_root: Path, db_path: Path | None = None) -> IndexLocation:
    """Where the index for ``repo_root`` lives: ``db_path`` if given, else the default."""
    if db_path is not None:
        # abspath, not resolve(): a symlinked final component must stay visible
        # so prepare_index can reject it.
        return IndexLocation(Path(os.path.abspath(db_path)), repo_local=False)  # noqa: PTH100
    return IndexLocation(repo_root / INDEX_DIR_NAME / INDEX_FILE_NAME, repo_local=True)


def _identity(info: os.stat_result) -> FileIdentity:
    return (info.st_dev, info.st_ino)


def _reject(path: Path, problem: str) -> IndexPathError:
    return IndexPathError(
        f"refusing to use index path {path}: {problem}. Remove it, or pass --db to store "
        "the index elsewhere."
    )


def _open_nofollow(
    name: str, flags: int, dir_fd: int | None, shown: Path, mode: int = 0o644
) -> int:
    try:
        return os.open(name, flags | _O_NOFOLLOW | _O_NONBLOCK, mode, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
            raise
        # The errno for "final component is a symlink" differs between
        # platforms, so classify the entry itself (without following it).
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            raise exc from None
        if stat.S_ISLNK(info.st_mode):
            raise _reject(shown, "it is a symbolic link") from exc
        if flags & _O_DIRECTORY:
            raise _reject(shown, "it is not a directory") from exc
        raise _reject(shown, "it is not a regular file") from exc


def _require_regular(fd: int, shown: Path) -> FileIdentity:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _reject(shown, "it is not a regular file")
    return _identity(info)


def _check_sidecars(db: Path, dir_fd: int | None) -> None:
    """SQLite sidecar files must be absent or regular files (never symlinks)."""
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        name = db.name + suffix
        try:
            if dir_fd is not None:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            else:
                info = os.lstat(db.with_name(name))
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise _reject(
                db.with_name(name), "unexpected SQLite sidecar that is not a regular file"
            )


@dataclass(frozen=True)
class PreparedIndex:
    """Identities captured while validating; re-checked after SQLite opens the file."""

    location: IndexLocation
    dir_identity: FileIdentity | None
    file_identity: FileIdentity | None

    def verify_unchanged(self) -> None:
        """Fail if the directory or database file was replaced since validation."""
        path = self.location.path
        try:
            file_info = os.lstat(path)
            # Only the repository-local index directory is pinned; an explicit
            # --db path may legitimately live under a symlinked directory.
            dir_info = os.lstat(path.parent) if self.dir_identity is not None else None
        except FileNotFoundError as exc:
            raise _reject(path, "it disappeared while being opened") from exc
        if stat.S_ISLNK(file_info.st_mode) or (
            dir_info is not None and stat.S_ISLNK(dir_info.st_mode)
        ):
            raise _reject(path, "it was replaced by a symbolic link while being opened")
        if self.file_identity is not None and _identity(file_info) != self.file_identity:
            raise _reject(path, "the file was replaced while being opened")
        if dir_info is not None and _identity(dir_info) != self.dir_identity:
            raise _reject(path, "the directory was replaced while being opened")


def _prepare_repo_local(location: IndexLocation, create: bool) -> PreparedIndex:
    db = location.path
    index_dir = db.parent
    repo_root = index_dir.parent
    if not _HAVE_DIR_FD:  # pragma: no cover - Windows
        return _prepare_lstat_only(location, create)

    root_fd = os.open(repo_root, os.O_RDONLY | _O_DIRECTORY)
    try:
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(INDEX_DIR_NAME, 0o755, dir_fd=root_fd)
        try:
            dir_fd = _open_nofollow(INDEX_DIR_NAME, os.O_RDONLY | _O_DIRECTORY, root_fd, index_dir)
        except FileNotFoundError:
            if create:
                raise
            return PreparedIndex(location, None, None)
    finally:
        os.close(root_fd)

    try:
        dir_identity = _identity(os.fstat(dir_fd))
        if create:
            _ensure_marker(dir_fd, index_dir / MARKER_NAME)
        flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
        try:
            fd = _open_nofollow(INDEX_FILE_NAME, flags, dir_fd, db)
        except FileNotFoundError:
            return PreparedIndex(location, dir_identity, None)
        try:
            file_identity = _require_regular(fd, db)
        finally:
            os.close(fd)
        _check_sidecars(db, dir_fd)
        return PreparedIndex(location, dir_identity, file_identity)
    finally:
        os.close(dir_fd)


def _ensure_marker(dir_fd: int, shown: Path) -> None:
    """Create ``.hebra/.gitignore`` exclusively; never follow or rewrite an existing one."""
    try:
        fd = _open_nofollow(MARKER_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd, shown)
    except FileExistsError:
        fd = _open_nofollow(MARKER_NAME, os.O_RDONLY, dir_fd, shown)
        try:
            _require_regular(fd, shown)
        finally:
            os.close(fd)
        return
    try:
        os.write(fd, b"*\n")
    finally:
        os.close(fd)


def _prepare_explicit(location: IndexLocation, create: bool) -> PreparedIndex:
    db = location.path
    if create:
        db.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
    try:
        fd = _open_nofollow(str(db), flags, None, db)
    except FileNotFoundError:
        return PreparedIndex(location, None, None)
    try:
        file_identity = _require_regular(fd, db)
    finally:
        os.close(fd)
    _check_sidecars(db, None)
    # Parent directories of an explicit path may legitimately be symlinks
    # (for example /tmp on macOS), so only the file itself is re-checked.
    return PreparedIndex(location, None, file_identity)


def _prepare_lstat_only(location: IndexLocation, create: bool) -> PreparedIndex:  # pragma: no cover
    db = location.path
    for candidate in (db.parent, db, db.parent / MARKER_NAME):
        if candidate.is_symlink():
            raise _reject(candidate, "it is a symbolic link")
    if create:
        db.parent.mkdir(exist_ok=True)
        marker = db.parent / MARKER_NAME
        if location.repo_local and not marker.exists():
            marker.write_text("*\n", encoding="utf-8")
    if not db.exists():
        return PreparedIndex(location, None, None)
    if not db.is_file():
        raise _reject(db, "it is not a regular file")
    return PreparedIndex(location, None, _identity(db.lstat()))


def prepare_index(location: IndexLocation, *, create: bool) -> PreparedIndex:
    """Validate (and with ``create``, safely create) the index path before SQLite opens it.

    Raises :class:`IndexPathError` if the path is unsafe. With ``create=False`` a
    missing index is reported through ``PreparedIndex.file_identity is None``.
    """
    if location.repo_local:
        return _prepare_repo_local(location, create)
    return _prepare_explicit(location, create)
