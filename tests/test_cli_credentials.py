from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import bumpcheck.cli as cli
from bumpcheck.bench.github import GitHubAuthError, RateLimitWaitTooLong

runner = CliRunner()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("BUMPCHECK_ENV_FILE", raising=False)
    return tmp_path


def _inspect_args(tmp_path: Path, *extra: str) -> list[str]:
    return ["bench", "inspect", "a/b#1", "--dir", str(tmp_path), *extra]


def test_missing_token_inspect_warns_once_and_exits_two(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_inspect", lambda *args, **kwargs: pytest.fail("ran"))

    result = runner.invoke(cli.app, _inspect_args(isolated))

    assert result.exit_code == 2
    assert result.output.count(cli.MISSING_TOKEN_MESSAGE) == 1


def test_missing_token_mine_warns_once_and_writes_nothing(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_mine", lambda **kwargs: pytest.fail("ran"))
    out = isolated / "out"

    result = runner.invoke(cli.app, ["bench", "mine", "--out", str(out)])

    assert result.exit_code == 2
    assert result.output.count(cli.MISSING_TOKEN_MESSAGE) == 1
    assert not out.exists()


def test_allow_unauthenticated_continues_and_still_warns(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_inspect", lambda *args, **kwargs: "report\n")

    result = runner.invoke(cli.app, _inspect_args(isolated, "--allow-unauthenticated"))

    assert result.exit_code == 0
    assert result.output.count(cli.MISSING_TOKEN_MESSAGE) == 1
    assert "report" in result.output


def test_offline_skips_the_guard(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_inspect", lambda *args, **kwargs: "report\n")

    result = runner.invoke(cli.app, _inspect_args(isolated, "--offline"))

    assert result.exit_code == 0
    assert cli.MISSING_TOKEN_MESSAGE not in result.output


def test_missing_env_file_exits_two(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = isolated / "gone.env"
    monkeypatch.setenv("BUMPCHECK_ENV_FILE", str(missing))

    result = runner.invoke(cli.app, _inspect_args(isolated))

    assert result.exit_code == 2
    assert "BUMPCHECK_ENV_FILE file does not exist" in result.output


def test_env_file_token_is_loaded_and_reported_verbosely(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = isolated / ".env"
    env_path.write_text("GITHUB_TOKEN=super-secret-token\n", encoding="utf-8")
    monkeypatch.setattr(cli, "run_inspect", lambda *args, **kwargs: "report\n")

    result = runner.invoke(cli.app, ["--verbose", *_inspect_args(isolated)])

    assert result.exit_code == 0
    assert os.environ["GITHUB_TOKEN"] == "super-secret-token"
    assert f"GITHUB_TOKEN loaded from {env_path}" in result.output
    assert "super-secret-token" not in result.output


def test_auth_error_exits_two_without_token_text(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-token")

    def fail(*args: object, **kwargs: object) -> str:
        raise GitHubAuthError("GitHub rejected the token (401): it is missing, expired or revoked")

    monkeypatch.setattr(cli, "run_inspect", fail)

    result = runner.invoke(cli.app, _inspect_args(isolated))

    assert result.exit_code == 2
    assert "GitHub rejected the token (401)" in result.output
    assert "super-secret-token" not in result.output


def test_rate_limit_wait_too_long_exits_three(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def fail(*args: object, **kwargs: object) -> str:
        raise RateLimitWaitTooLong(
            "rate limit wait is 60.0 minutes (until 2026-01-01 00:00:00 local); "
            "re-run after that time, or pass --max-rate-limit-wait SECONDS to wait"
        )

    monkeypatch.setattr(cli, "run_inspect", fail)

    result = runner.invoke(cli.app, _inspect_args(isolated))

    assert result.exit_code == 3
    assert "rate limit wait is 60.0 minutes" in result.output


def test_mine_rate_limit_wait_too_long_exits_three(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    def fail(**kwargs: object) -> int:
        raise RateLimitWaitTooLong("rate limit wait is 60.0 minutes")

    monkeypatch.setattr(cli, "run_mine", fail)

    result = runner.invoke(cli.app, ["bench", "mine", "--out", str(isolated / "out")])

    assert result.exit_code == 3


def test_negative_max_wait_exits_two(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setattr(cli, "run_inspect", lambda *args, **kwargs: pytest.fail("ran"))

    result = runner.invoke(cli.app, _inspect_args(isolated, "--max-rate-limit-wait=-1"))

    assert result.exit_code == 2
    assert "--max-rate-limit-wait must be zero or greater" in result.output
