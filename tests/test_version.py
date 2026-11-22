from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import bumpcheck


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    return data["project"]["version"]


def test_get_version_uses_distribution_metadata(monkeypatch) -> None:
    seen: list[str] = []

    def fake_version(name: str) -> str:
        seen.append(name)
        return "1.2.3"

    monkeypatch.setattr(bumpcheck, "dist_version", fake_version)

    assert bumpcheck._get_version() == "1.2.3"
    assert seen == ["bumpcheck-py"]


def test_get_version_falls_back_to_pyproject(monkeypatch) -> None:
    def missing_version(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(bumpcheck, "dist_version", missing_version)

    assert bumpcheck._get_version() == _pyproject_version()


def test_get_version_unknown_when_no_metadata_or_pyproject(monkeypatch) -> None:
    def missing_version(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(bumpcheck, "dist_version", missing_version)
    monkeypatch.setattr(bumpcheck, "_pyproject_version", lambda: None)

    assert bumpcheck._get_version() == "0+unknown"
