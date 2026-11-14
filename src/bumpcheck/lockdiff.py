"""Compare two lockfiles and report dependency changes."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from bumpcheck.models import Bump, ParseResult

SUPPORTED_KINDS = ("uv.lock", "poetry.lock", "requirements")

_UV_PROJECT_SOURCE_KEYS = ("editable", "virtual", "directory", "path")

_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[[^\]]*\])?"
    r"\s*==\s*"
    r"(?P<version>[^;\s]+)$"
)
_HASH_RE = re.compile(r"(?:^|\s)--hash=\S+")
_URL_REQUIREMENT_RE = re.compile(r"\s@\s|^[^=\s]+@")


class LockParseError(Exception):
    """Raised when a lockfile cannot be parsed."""


@dataclass(frozen=True)
class _Extracted:
    versions: dict[str, str] = field(default_factory=dict)
    ignored: set[str] = field(default_factory=set)
    ambiguous: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def _normalize_text(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _add_version(
    versions_by_name: dict[str, set[str]],
    warnings: list[str],
    name: str,
    version: str,
    kind: str,
) -> None:
    try:
        Version(version)
    except InvalidVersion:
        warnings.append(f"Package '{name}' has invalid version {version!r} in {kind}; ignoring it")
        return
    versions_by_name.setdefault(name, set()).add(version)


def _extract_toml(text: str, kind: str) -> _Extracted:
    if not text.strip():
        raise LockParseError(f"The {kind} input is empty")

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LockParseError(f"Invalid TOML in {kind}: {exc}") from exc

    packages = data.get("package", [])
    if not isinstance(packages, list):
        raise LockParseError(f"Invalid {kind}: the 'package' key must be an array of tables")

    versions_by_name: dict[str, set[str]] = {}
    ignored: set[str] = set()
    warnings: list[str] = []

    for index, entry in enumerate(packages, start=1):
        if not isinstance(entry, dict):
            raise LockParseError(f"Invalid {kind}: [[package]] entry #{index} is not a table")

        raw_name = entry.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise LockParseError(f"Invalid {kind}: [[package]] entry #{index} has no name")
        name = canonicalize_name(raw_name)

        if kind == "uv.lock":
            source = entry.get("source")
            if isinstance(source, dict):
                project_key = next(
                    (key for key in _UV_PROJECT_SOURCE_KEYS if key in source),
                    None,
                )
                if project_key is not None:
                    warnings.append(
                        f"Skipping project package '{name}' with a {project_key!r} source in {kind}"
                    )
                    continue

        raw_version = entry.get("version")
        if raw_version is None:
            ignored.add(name)
            warnings.append(f"Package '{name}' has no version in {kind}; ignoring it")
            continue
        if not isinstance(raw_version, str):
            ignored.add(name)
            warnings.append(f"Package '{name}' has a non-string version in {kind}; ignoring it")
            continue

        before = len(warnings)
        _add_version(versions_by_name, warnings, name, raw_version, kind)
        if len(warnings) > before:
            ignored.add(name)

    ambiguous = {name for name, values in versions_by_name.items() if len(values) > 1}
    for name in sorted(ambiguous):
        warnings.append(f"Package '{name}' has multiple versions in {kind}; skipping it")

    versions = {
        name: next(iter(values))
        for name, values in versions_by_name.items()
        if name not in ambiguous and name not in ignored
    }
    return _Extracted(versions=versions, ignored=ignored, ambiguous=ambiguous, warnings=warnings)


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line and not buffer:
            continue
        buffer = f"{buffer} {line}".strip() if buffer else line
        if buffer.endswith("\\"):
            buffer = buffer[:-1].rstrip()
            continue
        lines.append(buffer)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def _extract_requirements(text: str, kind: str) -> _Extracted:
    versions_by_name: dict[str, set[str]] = {}
    ignored: set[str] = set()
    warnings: list[str] = []

    for logical_line in _logical_lines(text):
        line = logical_line.split("#", 1)[0].strip()
        if not line:
            continue
        line = _HASH_RE.sub("", line).strip()
        if not line:
            continue
        if line.startswith("-"):
            continue
        if _URL_REQUIREMENT_RE.search(line):
            warnings.append(f"URL requirement {line!r} in {kind} is not supported; skipping it")
            continue

        requirement = line.split(";", 1)[0].strip()
        match = _REQUIREMENT_RE.match(requirement)
        if match is None:
            warnings.append(
                f"Requirement {requirement!r} in {kind} is not pinned with '=='; skipping it"
            )
            continue

        name = canonicalize_name(match.group("name"))
        version = match.group("version")
        before = len(warnings)
        _add_version(versions_by_name, warnings, name, version, kind)
        if len(warnings) > before:
            ignored.add(name)

    ambiguous = {name for name, values in versions_by_name.items() if len(values) > 1}
    for name in sorted(ambiguous):
        warnings.append(f"Package '{name}' has multiple versions in {kind}; skipping it")

    versions = {
        name: next(iter(values))
        for name, values in versions_by_name.items()
        if name not in ambiguous and name not in ignored
    }
    return _Extracted(versions=versions, ignored=ignored, ambiguous=ambiguous, warnings=warnings)


_EXTRACTORS = {
    "uv.lock": _extract_toml,
    "poetry.lock": _extract_toml,
    "requirements": _extract_requirements,
}


def parse_lock_pair(old_text: str, new_text: str, kind: str) -> ParseResult:
    """Compare two lockfile contents and return the changed packages."""
    extractor = _EXTRACTORS.get(kind)
    if extractor is None:
        supported = ", ".join(SUPPORTED_KINDS)
        raise LockParseError(f"Unknown lockfile kind {kind!r}; expected one of: {supported}")

    old = extractor(_normalize_text(old_text), kind)
    new = extractor(_normalize_text(new_text), kind)

    ignored = old.ignored | new.ignored
    ambiguous = old.ambiguous | new.ambiguous

    bumps: list[Bump] = []
    for name in sorted(set(old.versions) | set(new.versions)):
        if name in ignored or name in ambiguous:
            continue
        old_version = old.versions.get(name)
        new_version = new.versions.get(name)
        if old_version is not None and new_version is not None:
            if Version(old_version) == Version(new_version):
                continue
        bumps.append(
            Bump(
                package=name,
                old_version=old_version,
                new_version=new_version,
                source=kind,
            )
        )

    warnings = list(dict.fromkeys(old.warnings + new.warnings))
    return ParseResult(bumps=bumps, warnings=warnings)
