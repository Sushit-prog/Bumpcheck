"""BumpCheck package."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as dist_version
from pathlib import Path

_DISTRIBUTION_NAME = "bumpcheck-py"
_UNKNOWN_VERSION = "0+unknown"


def _pyproject_version() -> str | None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    value = project.get("version")
    return value if isinstance(value, str) else None


def _get_version() -> str:
    try:
        return dist_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _pyproject_version() or _UNKNOWN_VERSION


__version__ = _get_version()
