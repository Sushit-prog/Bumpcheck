"""Python-version compatibility checks for requires-python specs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

NonConcreteReason = Literal[
    "wildcard",
    "expression",
    "alias",
    "range",
    "implementation",
    "file",
    "unparsable",
]

_VERSION_FILE_RE = re.compile(r"python[-_]version[-_]file", re.IGNORECASE)
_WILDCARD_RE = re.compile(r"\d+\.[xX*](?:\.[A-Za-z0-9*]+)*")
_CONCRETE_RE = re.compile(
    r"(\d+)\.(\d+)(?:\.\d+)?(?:[tT]|[-_][A-Za-z0-9][A-Za-z0-9.]*)?",
    re.IGNORECASE,
)
_IMPLEMENTATION_RE = re.compile(r"(?:pypy|cpython)-", re.IGNORECASE)
_ALIASES = frozenset({"stable", "latest"})


@dataclass(frozen=True)
class ConcretePythonVersion:
    major: int
    minor: int

    def as_version(self) -> Version:
        return Version(f"{self.major}.{self.minor}")


@dataclass(frozen=True)
class NonConcretePythonVersion:
    raw: str
    reason: NonConcreteReason


PythonVersionResult = ConcretePythonVersion | NonConcretePythonVersion
PythonVersionInput = str | Version | PythonVersionResult
MetadataState = Literal["available", "unavailable"]

CPYTHON_MINORS: tuple[Version, ...] = tuple(Version(f"3.{minor}") for minor in range(8, 15))


@dataclass(frozen=True)
class RequiresPythonAssessment:
    old: str | None
    new: str | None
    state: MetadataState
    decided: bool
    tightened: bool
    excluded: tuple[str, ...]
    warnings: tuple[str, ...]


def interpret_python_version(
    value: object,
    *,
    source: Literal["version", "file"] = "version",
) -> PythonVersionResult:
    """Interpret a version scalar without fabricating a concrete version."""
    raw = value.strip() if isinstance(value, str) else ""
    if source == "file" or _VERSION_FILE_RE.fullmatch(raw):
        return NonConcretePythonVersion(raw=raw, reason="file")
    if not raw:
        return NonConcretePythonVersion(raw=raw, reason="unparsable")
    if "${{" in raw or "}}" in raw:
        return NonConcretePythonVersion(raw=raw, reason="expression")
    if raw.lower() in _ALIASES:
        return NonConcretePythonVersion(raw=raw, reason="alias")
    if _IMPLEMENTATION_RE.match(raw):
        return NonConcretePythonVersion(raw=raw, reason="implementation")
    if (
        any(token in raw for token in (">", "<", "=", "~", ",", "||"))
        or re.search(r"\s+-\s+", raw) is not None
    ):
        return NonConcretePythonVersion(raw=raw, reason="range")
    if re.fullmatch(r"\d+", raw) or _WILDCARD_RE.fullmatch(raw):
        return NonConcretePythonVersion(raw=raw, reason="wildcard")
    match = _CONCRETE_RE.fullmatch(raw)
    if match is not None:
        return ConcretePythonVersion(major=int(match.group(1)), minor=int(match.group(2)))
    return NonConcretePythonVersion(raw=raw, reason="unparsable")


def _as_version(value: PythonVersionInput) -> Version | None:
    if isinstance(value, Version):
        return value
    if isinstance(value, ConcretePythonVersion):
        return value.as_version()
    if isinstance(value, NonConcretePythonVersion):
        return None
    if isinstance(value, str):
        return normalize_python_version(value)
    return None


def normalize_python_version(text: str) -> Version | None:
    """Parse a concrete Python version; non-concrete values yield None."""
    result = interpret_python_version(text)
    if isinstance(result, ConcretePythonVersion):
        return result.as_version()
    return None


def allows_python(
    requires_python: str | None,
    python_version: PythonVersionInput,
) -> bool | None:
    """Whether a requires-python spec allows a version.

    Returns None when the spec is missing or malformed, or when the version
    is non-concrete. Never raises.
    """
    if not isinstance(requires_python, str) or not requires_python.strip():
        return None
    version = _as_version(python_version)
    if version is None:
        return None
    try:
        return version in SpecifierSet(requires_python)
    except InvalidSpecifier:
        return None


def python_floor(requires_python: str | None) -> Version | None:
    """Lowest Python 3+ version allowed; None when absent, unbounded, or unknown."""
    if not isinstance(requires_python, str) or not requires_python.strip():
        return None
    try:
        spec_set = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return None
    if not any(specifier.operator in {">", ">=", "~=", "=="} for specifier in spec_set):
        return None
    for major in (3, 4, 5):
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
    versions: Iterable[PythonVersionInput],
) -> list[str]:
    """Versions the old spec allows and the new spec no longer allows.

    Non-concrete versions are skipped. Missing or malformed specs yield an
    empty list. Never raises.
    """
    result: list[str] = []
    for raw in versions:
        version = _as_version(raw)
        if version is None:
            continue
        old_ok = allows_python(old_requires, version)
        new_ok = allows_python(new_requires, version)
        if old_ok is True and new_ok is False:
            result.append(str(version))
    return result


def _normalized_specifier(value: str | None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _specifier_is_valid(value: str | None) -> bool:
    if value is None:
        return True
    try:
        SpecifierSet(value)
    except InvalidSpecifier:
        return False
    return True


def assess_requires_python_change(
    old: str | None,
    new: str | None,
    versions: Iterable[PythonVersionInput] = (),
    *,
    state: MetadataState = "available",
) -> RequiresPythonAssessment:
    """Assess whether a requires-python change removes previously allowed versions."""
    old_spec = _normalized_specifier(old)
    new_spec = _normalized_specifier(new)
    warnings: list[str] = []

    if state == "unavailable":
        warnings.append("requires_python metadata unavailable")
        return RequiresPythonAssessment(
            old=old_spec,
            new=new_spec,
            state=state,
            decided=False,
            tightened=False,
            excluded=(),
            warnings=tuple(warnings),
        )

    if not _specifier_is_valid(old_spec):
        warnings.append(f"invalid old requires_python specifier: {old_spec}")
    if not _specifier_is_valid(new_spec):
        warnings.append(f"invalid new requires_python specifier: {new_spec}")
    if warnings:
        return RequiresPythonAssessment(
            old=old_spec,
            new=new_spec,
            state=state,
            decided=False,
            tightened=False,
            excluded=(),
            warnings=tuple(warnings),
        )

    candidates = list(CPYTHON_MINORS)
    for raw in versions:
        version = _as_version(raw)
        if version is not None and version not in candidates:
            candidates.append(version)

    excluded_versions: list[Version] = []
    for version in candidates:
        old_ok = True if old_spec is None else allows_python(old_spec, version)
        new_ok = True if new_spec is None else allows_python(new_spec, version)
        if old_ok is True and new_ok is False:
            excluded_versions.append(version)

    excluded = tuple(
        str(version)
        for version in sorted(set(excluded_versions), key=lambda item: (item.major, item.minor))
    )
    return RequiresPythonAssessment(
        old=old_spec,
        new=new_spec,
        state=state,
        decided=True,
        tightened=bool(excluded),
        excluded=excluded,
        warnings=tuple(warnings),
    )
