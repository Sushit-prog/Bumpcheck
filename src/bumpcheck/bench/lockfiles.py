"""Lockfile path classification shared with ``scripts/realworld.py``."""

from __future__ import annotations

import fnmatch

REQUIREMENTS_GLOB = "requirements*.txt"


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def lockfile_kind(path: str) -> str | None:
    """Return the parser kind for a repo path, or None if it is not a lockfile."""
    basename = _basename(path)
    if basename == "uv.lock":
        return "uv.lock"
    if basename == "poetry.lock":
        return "poetry.lock"
    if fnmatch.fnmatchcase(basename, REQUIREMENTS_GLOB):
        return "requirements"
    return None


def select_lockfiles(files: list[dict]) -> list[dict]:
    """Keep only the changed files that are lockfiles, preserving order."""
    return [entry for entry in files if lockfile_kind(str(entry.get("filename", ""))) is not None]
