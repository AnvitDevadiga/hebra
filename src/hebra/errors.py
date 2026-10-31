"""Structured error types.

Every error Hebra raises on purpose derives from :class:`HebraError`; the CLI
turns these into a one-line message and a non-zero exit code. Anything else
reaching the CLI is a bug and is allowed to surface with a traceback.
"""


class HebraError(Exception):
    """Base class for expected, user-facing failures."""


class RepositoryError(HebraError):
    """The repository path is missing, unreadable, or not a directory."""


class IndexNotFoundError(HebraError):
    """No index exists for the repository yet."""


class IndexVersionError(HebraError):
    """The index was written by an incompatible schema version."""


class QueryError(HebraError):
    """The task text yields no searchable terms."""


class BudgetError(HebraError):
    """The token budget is invalid or too small for any output."""


class TokenizerError(HebraError):
    """The configured tokenizer could not be loaded."""


class IndexPathError(HebraError):
    """The index location is unsafe (symlink, wrong file type, foreign database)."""


class IndexDatabaseError(HebraError):
    """The index database is corrupt, locked, or otherwise unusable."""


class GitError(HebraError):
    """Git metadata is present but the file list could not be obtained safely."""


class OutputPathError(HebraError):
    """An output file could not be written safely."""


class RepositoryChangedError(HebraError):
    """Source files kept changing while the repository was being indexed."""
