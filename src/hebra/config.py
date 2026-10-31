"""Explicit configuration with documented defaults."""

from __future__ import annotations

from dataclasses import dataclass, field

from hebra.paths import INDEX_DIR_NAME

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
        # Credential stores
        ".ssh",
        ".aws",
        ".gnupg",
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


# File-name patterns (fnmatch, matched case-insensitively against the base
# name) that commonly hold credentials. Name filters are a safety net, not
# secret detection: a secret in an ordinary file name is still indexed.
DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "credentials*",
    "secrets.yaml",
    "secrets.yml",
    "secrets.json",
)

# Source-code suffixes exempt from the broad ``credentials*`` pattern, so a
# module such as ``credentials.py`` stays indexable.
CODE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".sh",
        ".md",
        ".rst",
    }
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
    sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS
    # Upper bound on paths enumerated (directory entries walked, or paths read
    # from `git ls-files`), applied before max_files.
    max_scan_entries: int = 200_000
    # In Git repositories: use `git ls-files` (False walks the filesystem and
    # does NOT apply .gitignore). Ignored outside Git repositories.
    use_git: bool = True
    # In Git repositories: also index untracked files that .gitignore does not
    # exclude. Off by default so new, never-committed files are not indexed.
    include_untracked: bool = False


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
