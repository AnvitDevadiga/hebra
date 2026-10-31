from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from hebra.config import IndexConfig, ScanConfig
from hebra.indexing import IndexReport, index_repository

WriteRepo = Callable[[dict[str, str | bytes]], Path]

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not installed")


def write_files(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")


@pytest.fixture
def make_repo(tmp_path: Path) -> WriteRepo:
    """Create a plain (non-Git) directory populated with ``files``."""

    def factory(files: dict[str, str | bytes]) -> Path:
        root = tmp_path / "repo"
        root.mkdir(exist_ok=True)
        write_files(root, files)
        return root

    return factory


def git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    result = subprocess.run(
        ["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def make_git_repo(tmp_path: Path) -> WriteRepo:
    """Create a Git repository with ``files`` committed."""

    def factory(files: dict[str, str | bytes]) -> Path:
        root = tmp_path / "gitrepo"
        root.mkdir(exist_ok=True)
        git(root, "init", "-q", "-b", "main")
        write_files(root, files)
        git(root, "add", "-A")
        git(root, "commit", "-q", "--allow-empty", "-m", "init")
        return root

    return factory


def fs_index(root: Path, **scan: object) -> IndexReport:
    """Index without Git so tests are independent of the git binary."""
    return index_repository(root, config=IndexConfig(scan=ScanConfig(use_git=False, **scan)))  # type: ignore[arg-type]


SAMPLE_PY = '''\
"""Configuration loading utilities."""

import os
from pathlib import Path
from . import helpers as h


class ConfigLoader:
    """Load configuration files from disk."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load_file(self, name: str) -> str:
        """Read one config file."""
        return (self.root / name).read_text()

    class Cache:
        def get(self, key):
            return None


async def fetch_remote_config(url: str) -> dict:
    """Fetch configuration over the network."""

    def parse(payload):
        return payload

    return parse({})


@staticmethod
def decorated_helper():
    return os.getcwd()
'''
