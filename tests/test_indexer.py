from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import SAMPLE_PY, fs_index, git, requires_git
from hebra.errors import (
    IndexDatabaseError,
    IndexNotFoundError,
    IndexVersionError,
    RepositoryError,
)
from hebra.indexing import index_repository, index_summary
from hebra.indexing.store import SCHEMA_VERSION


def default_index_path(root: Path) -> Path:
    return root / ".hebra" / "index.sqlite3"


def table_counts(root: Path) -> dict[str, int]:
    conn = sqlite3.connect(default_index_path(root))
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("files", "symbols", "imports", "chunks", "chunks_fts")
        }
    finally:
        conn.close()


def test_index_basic_repository(make_repo) -> None:
    root = make_repo({"pkg/config.py": SAMPLE_PY, "README.md": "# Demo\n\nSome docs.\n"})
    report = fs_index(root)
    assert report.files_indexed == 2
    assert report.files_added == 2
    assert report.file_source == "filesystem"
    counts = table_counts(root)
    assert counts["files"] == 2
    assert counts["symbols"] == 8
    assert counts["imports"] == 3
    assert counts["chunks"] == counts["chunks_fts"] > 0
    assert (root / ".hebra" / ".gitignore").read_text() == "*\n"


def test_empty_repository(make_repo) -> None:
    root = make_repo({})
    report = fs_index(root)
    assert report.files_indexed == 0
    assert table_counts(root)["chunks"] == 0
    assert index_summary(root)["files"] == 0


def test_reindex_unchanged_does_not_duplicate(make_repo) -> None:
    root = make_repo({"a.py": SAMPLE_PY, "b.txt": "hello\n"})
    fs_index(root)
    before = table_counts(root)
    report = fs_index(root)
    assert report.files_unchanged == 2
    assert report.files_added == report.files_changed == report.chunks_written == 0
    assert table_counts(root) == before


def test_changed_file_replaces_rows(make_repo) -> None:
    root = make_repo({"a.py": "def old_name():\n    pass\n", "b.py": "x = 1\n"})
    fs_index(root)
    (root / "a.py").write_text("def new_name():\n    pass\n\ndef another():\n    pass\n")
    report = fs_index(root)
    assert (report.files_changed, report.files_unchanged) == (1, 1)
    conn = sqlite3.connect(default_index_path(root))
    names = [r[0] for r in conn.execute("SELECT name FROM symbols ORDER BY name")]
    fts_rows = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    chunk_rows = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    assert names == ["another", "new_name"]
    assert fts_rows == chunk_rows


def test_deleted_file_is_removed(make_repo) -> None:
    root = make_repo({"a.py": "x = 1\n", "b.py": "y = 2\n"})
    fs_index(root)
    (root / "b.py").unlink()
    report = fs_index(root)
    assert report.files_removed == 1
    counts = table_counts(root)
    assert counts["files"] == 1
    assert counts["chunks"] == counts["chunks_fts"] == 1


def test_newly_excluded_file_is_removed(make_repo) -> None:
    root = make_repo({"a.py": "x = 1\n", "big.py": "y = 2\n" * 500})
    fs_index(root)
    report = fs_index(root, max_file_bytes=100)
    assert report.files_removed == 1
    assert report.skipped == {"too-large": 1}


def test_syntax_error_falls_back_to_text(make_repo) -> None:
    root = make_repo({"broken.py": "def oops(:\n    pass\n"})
    report = fs_index(root)
    assert report.python_parse_failures == 1
    assert any("failed to parse" in w for w in report.warnings)
    summary = index_summary(root)
    assert summary["files_by_parser"] == {"text-fallback": 1}
    assert summary["symbols"] == 0


def test_summary_reports_languages_and_kinds(make_repo) -> None:
    root = make_repo({"a.py": SAMPLE_PY, "notes.md": "# Notes\n"})
    fs_index(root)
    summary = index_summary(root)
    assert summary["files_by_language"] == {"markdown": 1, "python": 1}
    assert summary["symbols_by_kind"]["method"] == 3
    assert summary["head_revision"] is None
    assert summary["last_run"]["file_source"] == "filesystem"


def test_missing_repository_path(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError):
        index_repository(tmp_path / "nope")
    (tmp_path / "file.txt").write_text("x")
    with pytest.raises(RepositoryError):
        index_repository(tmp_path / "file.txt")


def test_summary_without_index(tmp_path: Path) -> None:
    with pytest.raises(IndexNotFoundError):
        index_summary(tmp_path)


def test_schema_mismatch_rebuilds_on_index_and_errors_on_read(make_repo) -> None:
    root = make_repo({"a.py": "x = 1\n"})
    fs_index(root)
    db = default_index_path(root)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(IndexVersionError):
        index_summary(root)
    report = fs_index(root)
    assert report.files_added == 1
    assert index_summary(root)["schema_version"] == SCHEMA_VERSION


def test_corrupt_index_file(make_repo) -> None:
    root = make_repo({"a.py": "x = 1\n"})
    db = default_index_path(root)
    db.parent.mkdir()
    db.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(IndexDatabaseError, match="not a usable SQLite database"):
        index_summary(root)
    # Writing must fail closed too, leaving the file untouched.
    with pytest.raises(IndexDatabaseError):
        fs_index(root)
    assert db.read_bytes() == b"this is not sqlite" * 100


@requires_git
def test_records_git_head_revision(make_git_repo) -> None:
    root = make_git_repo({"a.py": "x = 1\n"})
    report = index_repository(root)
    assert report.file_source == "git"
    assert report.head_revision == git(root, "rev-parse", "HEAD")
    assert index_summary(root)["head_revision"] == report.head_revision


@requires_git
def test_index_directory_is_invisible_to_git(make_git_repo) -> None:
    root = make_git_repo({"a.py": "x = 1\n"})
    index_repository(root)
    assert git(root, "status", "--porcelain") == ""
