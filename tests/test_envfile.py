from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bumpcheck.envfile import EnvFileError, has_token, load_env

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


def test_real_env_with_token_beats_every_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(tmp_path / ".env", "GITHUB_TOKEN=from-cwd\n")
    _write(home / ".bumpcheck" / ".env", "GITHUB_TOKEN=from-home\n")
    environ = {"GITHUB_TOKEN": "from-real-env"}

    assert load_env(environ=environ, cwd=tmp_path, home=home) is None
    assert environ["GITHUB_TOKEN"] == "from-real-env"


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_real_env_is_treated_as_unset(tmp_path: Path, empty: str) -> None:
    _write(tmp_path / ".env", "GITHUB_TOKEN=from-file\n")
    environ = {"GITHUB_TOKEN": empty}

    loaded = load_env(environ=environ, cwd=tmp_path, home=tmp_path / "home")

    assert loaded == tmp_path / ".env"
    assert environ["GITHUB_TOKEN"] == "from-file"


def test_override_file_beats_home_and_cwd(tmp_path: Path) -> None:
    home = tmp_path / "home"
    override = _write(tmp_path / "override.env", "GITHUB_TOKEN=from-override\n")
    _write(home / ".bumpcheck" / ".env", "GITHUB_TOKEN=from-home\n")
    _write(tmp_path / ".env", "GITHUB_TOKEN=from-cwd\n")
    environ = {"BUMPCHECK_ENV_FILE": str(override)}

    assert load_env(environ=environ, cwd=tmp_path, home=home) == override
    assert environ["GITHUB_TOKEN"] == "from-override"


def test_home_beats_cwd(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home_env = _write(home / ".bumpcheck" / ".env", "GITHUB_TOKEN=from-home\n")
    _write(tmp_path / ".env", "GITHUB_TOKEN=from-cwd\n")
    environ: dict[str, str] = {}

    assert load_env(environ=environ, cwd=tmp_path, home=home) == home_env
    assert environ["GITHUB_TOKEN"] == "from-home"


def test_only_github_token_is_loaded(tmp_path: Path) -> None:
    _write(
        tmp_path / ".env",
        "GITHUB_TOKEN=from-file\nPATH=/evil\nPYTHONPATH=/evil\nHTTP_PROXY=http://evil\n",
    )
    environ: dict[str, str] = {}

    load_env(environ=environ, cwd=tmp_path, home=tmp_path / "home")

    assert environ["GITHUB_TOKEN"] == "from-file"
    assert "PATH" not in environ
    assert "PYTHONPATH" not in environ
    assert "HTTP_PROXY" not in environ


def test_parses_export_quotes_bom_and_crlf(tmp_path: Path) -> None:
    _write(
        tmp_path / ".env",
        "# comment\r\nexport GITHUB_TOKEN='single-quoted'\r\n",
        encoding="utf-8-sig",
    )
    environ: dict[str, str] = {}
    load_env(environ=environ, cwd=tmp_path, home=tmp_path / "home")
    assert environ["GITHUB_TOKEN"] == "single-quoted"

    _write(tmp_path / ".env", 'GITHUB_TOKEN="double-quoted"\n')
    environ = {}
    load_env(environ=environ, cwd=tmp_path, home=tmp_path / "home")
    assert environ["GITHUB_TOKEN"] == "double-quoted"

    _write(tmp_path / ".env", "GITHUB_TOKEN=plain\n")
    environ = {}
    load_env(environ=environ, cwd=tmp_path, home=tmp_path / "home")
    assert environ["GITHUB_TOKEN"] == "plain"


def test_no_variable_expansion_or_inline_comment_stripping(tmp_path: Path) -> None:
    _write(tmp_path / ".env", "GITHUB_TOKEN=abc$HOME#def\n")
    environ: dict[str, str] = {}
    load_env(environ=environ, cwd=tmp_path, home=tmp_path / "home")
    assert environ["GITHUB_TOKEN"] == "abc$HOME#def"


def test_malformed_lines_are_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path / ".env",
        "NOEQUALS\n=missing-key\nGITHUB_TOKEN\n1BAD=x\nGITHUB_TOKEN=good\n",
    )
    environ: dict[str, str] = {}
    load_env(environ=environ, cwd=tmp_path, home=tmp_path / "home")
    assert environ["GITHUB_TOKEN"] == "good"


def test_empty_values_do_not_block_later_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    override = _write(tmp_path / "override.env", "GITHUB_TOKEN=\n")
    home_env = _write(home / ".bumpcheck" / ".env", "GITHUB_TOKEN=from-home\n")
    environ = {"BUMPCHECK_ENV_FILE": str(override)}

    assert load_env(environ=environ, cwd=tmp_path, home=home) == home_env
    assert environ["GITHUB_TOKEN"] == "from-home"


def test_missing_files_are_fine(tmp_path: Path) -> None:
    messages: list[str] = []
    environ: dict[str, str] = {}
    assert (
        load_env(environ=environ, cwd=tmp_path, home=tmp_path / "home", warn=messages.append)
        is None
    )
    assert environ == {}
    assert messages == []


def test_missing_override_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "gone.env"
    with pytest.raises(EnvFileError) as excinfo:
        load_env(environ={"BUMPCHECK_ENV_FILE": str(missing)}, cwd=tmp_path)
    assert "BUMPCHECK_ENV_FILE file does not exist" in str(excinfo.value)


def test_verbose_reports_the_file_but_never_the_value(tmp_path: Path) -> None:
    path = _write(tmp_path / ".env", "GITHUB_TOKEN=super-secret-token\n")
    messages: list[str] = []
    environ: dict[str, str] = {}

    load_env(
        verbose=True, environ=environ, cwd=tmp_path, home=tmp_path / "home", warn=messages.append
    )

    assert messages == [f"GITHUB_TOKEN loaded from {path}"]
    assert all("super-secret-token" not in message for message in messages)


def test_no_value_leaks_to_captured_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / ".env", "GITHUB_TOKEN=super-secret-token\n")
    environ: dict[str, str] = {}
    load_env(verbose=True, environ=environ, cwd=tmp_path, home=tmp_path / "home")
    captured = capsys.readouterr()
    assert "super-secret-token" not in captured.out
    assert "super-secret-token" not in captured.err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_group_readable_file_warns(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    _write(path, "GITHUB_TOKEN=token\n")
    path.chmod(0o644)
    messages: list[str] = []

    load_env(environ={}, cwd=tmp_path, home=tmp_path / "home", warn=messages.append)

    assert len(messages) == 1
    assert "readable by group or others" in messages[0]
    assert "chmod 600" in messages[0]

    path.chmod(0o600)
    messages = []
    load_env(environ={}, cwd=tmp_path, home=tmp_path / "home", warn=messages.append)
    assert messages == []


def test_gitignore_hides_env_files() -> None:
    lines = {
        line.strip() for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert ".env" in lines
    assert ".env.*" in lines


def test_has_token_ignores_whitespace() -> None:
    assert has_token({"GITHUB_TOKEN": "x"}) is True
    assert has_token({"GITHUB_TOKEN": "  "}) is False
    assert has_token({}) is False
