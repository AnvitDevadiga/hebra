from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import SAMPLE_PY
from hebra import __version__
from hebra.cli import main
from hebra.context.tokenizer import get_token_counter


@pytest.fixture
def repo(make_repo) -> Path:
    return make_repo({"pkg/config.py": SAMPLE_PY, "README.md": "# Demo\n\nConfig docs.\n"})


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_full_workflow(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "index", str(repo), "--no-git")
    assert code == 0
    assert "2 indexed (2 added" in out

    code, out, _ = run(capsys, "search", str(repo), "--task", "config loader")
    assert code == 0
    assert "pkg/config.py:8-20  class ConfigLoader" in out

    code, out, err = run(capsys, "pack", str(repo), "--task", "config loader", "--budget", "500")
    assert code == 0
    assert out.startswith("# Hebra context")
    assert get_token_counter().count(out) <= 500
    assert "/500 tokens (o200k_base)" in err

    code, out, _ = run(capsys, "stats", str(repo))
    assert code == 0
    assert "Files:      2" in out


def test_json_outputs_parse(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "index", str(repo), "--no-git", "--json")
    assert code == 0 and json.loads(out)["files_indexed"] == 2
    code, out, _ = run(capsys, "search", str(repo), "--task", "config", "--json")
    assert code == 0 and json.loads(out)["results"][0]["path"] == "pkg/config.py"
    code, out, _ = run(
        capsys, "pack", str(repo), "--task", "config", "--budget", "800", "--format", "json"
    )
    assert code == 0 and json.loads(out)["budget"]["used"] <= 800
    code, out, _ = run(capsys, "stats", str(repo), "--json")
    assert code == 0 and json.loads(out)["files"] == 2


def test_pack_output_file(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "index", str(repo), "--no-git")
    target = tmp_path / "context.md"
    code, out, _ = run(
        capsys, "pack", str(repo), "--task", "config", "--budget", "800", "-o", str(target)
    )
    assert code == 0 and out == ""
    assert target.read_text(encoding="utf-8").startswith("# Hebra context")


def test_custom_db_path(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "elsewhere" / "idx.sqlite3"
    assert run(capsys, "index", str(repo), "--no-git", "--db", str(db))[0] == 0
    assert db.is_file()
    assert not (repo / ".hebra").exists()
    assert run(capsys, "search", str(repo), "--task", "config", "--db", str(db))[0] == 0


def test_missing_index_is_a_clean_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "search", str(repo), "--task", "config")
    assert code == 1
    assert out == ""
    assert err.startswith("hebra: error: no index found")


def test_missing_repository(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "index", str(tmp_path / "missing"))
    assert code == 1
    assert "does not exist" in err


@pytest.mark.parametrize("budget", ["0", "-5", "abc", "2.5"])
def test_invalid_budget_is_a_usage_error(
    repo: Path, budget: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["pack", str(repo), "--task", "x", "--budget", budget])
    assert exc.value.code == 2
    assert "--budget" in capsys.readouterr().err


def test_too_small_budget_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "index", str(repo), "--no-git")
    code, out, err = run(capsys, "pack", str(repo), "--task", "config", "--budget", "5")
    assert code == 1 and out == ""
    assert "too small" in err


@pytest.mark.parametrize("command", ["index", "search", "pack", "stats"])
def test_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])
    assert exc.value.code == 0
    assert "repository" in capsys.readouterr().out


def test_module_entry_point_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "hebra", "--version"], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == f"hebra {__version__}"
