"""Python-version compatibility checks for requires-python specs."""

from __future__ import annotations

import re
from collections.abc import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


def normalize_python_version(text: str) -> Version | None:
    """Parse a Python version value such as "3.9", "3.x" or "3.9.1"."""
    value = str(text).strip()
    if not value:
        return None
    if value.lower().endswith(".x"):
        value = f"{value[:-2]}.0"
    elif re.fullmatch(r"\d+", value):
        value = f"{value}.0"
    try:
        return Version(value)
    except InvalidVersion:
        return None


def allows_python(requires_python: str | None, python_version: str | Version) -> bool | None:
    """Whether a requires-python spec allows a version.

    Returns None when the spec is missing or malformed, or when the version
    cannot be parsed. Never raises.
    """
    if not isinstance(requires_python, str) or not requires_python.strip():
        return None
    version = (
        python_version
        if isinstance(python_version, Version)
        else normalize_python_version(str(python_version))
    )
    if version is None:
        return None
    try:
        return version in SpecifierSet(requires_python)
    except InvalidSpecifier:
        return None


def python_floor(requires_python: str | None) -> Version | None:
    """Lowest concrete version a requires-python spec allows; None when unknown."""
    if not isinstance(requires_python, str) or not requires_python.strip():
        return None
    try:
        spec_set = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return None
    for major in (2, 3, 4, 5):
        for minor in range(40):
            for text in (f"{major}.{minor}", f"{major}.{minor}.0"):
                try:
                    candidate = Version(text)
                except InvalidVersion:
                    continue
                if candidate in spec_set:
                    return Version(f"{major}.{minor}")
    return None


def newly_disallowed(
    old_requires: str | None,
    new_requires: str | None,
    versions: Iterable[str | Version],
) -> list[str]:
    """Versions the old spec allows and the new spec no longer allows.

    Versions that cannot be parsed are skipped. Missing or malformed specs
    yield an empty list. Never raises.
    """
    result: list[str] = []
    for raw in versions:
        version = raw if isinstance(raw, Version) else normalize_python_version(str(raw))
        if version is None:
            continue
        old_ok = allows_python(old_requires, version)
        new_ok = allows_python(new_requires, version)
        if old_ok is True and new_ok is False:
            result.append(str(version))
    return result
