"""Explicit configuration with documented defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

INDEX_DIR_NAME = ".hebra"
INDEX_FILE_NAME = "index.sqlite3"

# Directory names never descended into or indexed, at any depth.
DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        INDEX_DIR_NAME,
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "env",
        ".eggs",
        "node_modules",
        "bower_components",
        "site-packages",
        "build",
        "dist",
        "target",
        "out",
        ".next",
        ".nuxt",
        ".gradle",
        ".idea",
        ".vscode",
        "coverage",
        "htmlcov",
    }
)

# Exact file names that are generated or low-signal for code context.
DEFAULT_EXCLUDED_FILES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "composer.lock",
        "Gemfile.lock",
        "go.sum",
        ".DS_Store",
    }
)

# File-name suffixes that are binary or generated. Checked case-insensitively.
DEFAULT_EXCLUDED_SUFFIXES: tuple[str, ...] = (
    ".min.js",
    ".min.css",
    ".map",
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".o",
    ".a",
    ".class",
    ".jar",
    ".whl",
    ".egg",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".pdf",
    ".mp3",
    ".mp4",
    ".mov",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".pkl",
    ".npy",
    ".npz",
    ".parquet",
)


@dataclass(frozen=True)
class ScanConfig:
    """Limits and exclusions applied while discovering files."""

    max_file_bytes: int = 1_000_000
    max_files: int = 50_000
    max_total_bytes: int = 200_000_000
    excluded_dirs: frozenset[str] = DEFAULT_EXCLUDED_DIRS
    excluded_files: frozenset[str] = DEFAULT_EXCLUDED_FILES
    excluded_suffixes: tuple[str, ...] = DEFAULT_EXCLUDED_SUFFIXES
    use_git: bool = True


@dataclass(frozen=True)
class ChunkConfig:
    """Bounds on the size of indexed chunks."""

    max_lines: int = 120
    max_chars: int = 8_000
    text_window_lines: int = 60


@dataclass(frozen=True)
class IndexConfig:
    scan: ScanConfig = field(default_factory=ScanConfig)
    chunk: ChunkConfig = field(default_factory=ChunkConfig)


def default_index_path(repo_root: Path) -> Path:
    return repo_root / INDEX_DIR_NAME / INDEX_FILE_NAME
