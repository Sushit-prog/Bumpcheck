from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from bumpcheck.cli import app

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(kind_dir: str, scenario: str, name: str) -> str:
    return (FIXTURES / kind_dir / scenario / name).read_text(encoding="utf-8")


def write_lock(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_cli_parse_success(tmp_path: Path) -> None:
    old = write_lock(tmp_path / "old.lock", fixture_text("uv_lock", "single_bump", "old.lock"))
    new = write_lock(tmp_path / "new.lock", fixture_text("uv_lock", "single_bump", "new.lock"))

    result = CliRunner().invoke(app, ["parse", str(old), str(new), "--kind", "uv.lock"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["bumps"] == [
        {
            "package": "packaging",
            "old_version": "24.0",
            "new_version": "25.0",
            "source": "uv.lock",
        }
    ]
    assert payload["warnings"] == []


def test_cli_parse_includes_unresolved(tmp_path: Path) -> None:
    old = write_lock(
        tmp_path / "old.lock", fixture_text("uv_lock", "forked_one_side_changes", "old.lock")
    )
    new = write_lock(
        tmp_path / "new.lock", fixture_text("uv_lock", "forked_one_side_changes", "new.lock")
    )

    result = CliRunner().invoke(app, ["parse", str(old), str(new), "--kind", "uv.lock"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["bumps"] == []
    assert payload["unresolved"] == [{"package": "foo", "removed": ["1.0"], "added": ["1.1"]}]


def test_cli_parse_requirements(tmp_path: Path) -> None:
    old = write_lock(
        tmp_path / "old.txt", fixture_text("requirements", "extras_markers", "old.lock")
    )
    new = write_lock(
        tmp_path / "new.txt", fixture_text("requirements", "extras_markers", "new.lock")
    )

    result = CliRunner().invoke(app, ["parse", str(old), str(new), "--kind", "requirements"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [bump["package"] for bump in payload["bumps"]] == ["requests"]


def test_cli_parse_error_exits_with_code_2(tmp_path: Path) -> None:
    old = write_lock(tmp_path / "old.lock", '[[package]\nname = "x"\n')
    new = write_lock(tmp_path / "new.lock", fixture_text("uv_lock", "single_bump", "new.lock"))

    result = CliRunner().invoke(app, ["parse", str(old), str(new), "--kind", "uv.lock"])

    assert result.exit_code == 2
    assert "Invalid TOML" in result.output


def test_cli_parse_reads_utf8_sig(tmp_path: Path) -> None:
    old_path = tmp_path / "old.lock"
    old_path.write_text(
        fixture_text("uv_lock", "single_bump", "old.lock"),
        encoding="utf-8-sig",
        newline="\n",
    )
    new = write_lock(tmp_path / "new.lock", fixture_text("uv_lock", "single_bump", "new.lock"))

    result = CliRunner().invoke(app, ["parse", str(old_path), str(new), "--kind", "uv.lock"])

    assert result.exit_code == 0
    assert json.loads(result.output)["bumps"][0]["package"] == "packaging"


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "0.0.1" in result.output
