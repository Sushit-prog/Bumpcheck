"""Compare two lockfiles and report dependency changes."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from bumpcheck.models import Bump, DependencyPin, ParseResult, Unresolved

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
_PACKAGE_TABLE_RE = re.compile(r"^\s*\[\[package\]\]\s*(?:#.*)?$")
_TABLE_RE = re.compile(r"^\s*\[")
_TOML_NAME_RE = re.compile(r"^\s*name\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)")
_TOML_VERSION_RE = re.compile(r"^\s*version\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)")


class LockParseError(Exception):
    """Raised when a lockfile cannot be parsed."""


@dataclass(frozen=True)
class _Extracted:
    versions: dict[str, list[str]] = field(default_factory=dict)
    ignored: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def _normalize_text(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _add_version(
    versions_by_name: dict[str, list[str]],
    warnings: list[str],
    name: str,
    version: str,
    kind: str,
) -> None:
    try:
        parsed = Version(version)
    except InvalidVersion:
        warnings.append(f"Package '{name}' has invalid version {version!r} in {kind}; ignoring it")
        return
    existing = versions_by_name.setdefault(name, [])
    if all(Version(known) != parsed for known in existing):
        existing.append(version)


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

    versions_by_name: dict[str, list[str]] = {}
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
                    article = "an" if project_key[:1].lower() in "aeiou" else "a"
                    warnings.append(
                        f"Skipping project package '{name}' with {article} {project_key!r} "
                        f"source in {kind}"
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

    versions = {name: values for name, values in versions_by_name.items() if name not in ignored}
    return _Extracted(versions=versions, ignored=ignored, warnings=warnings)


def _logical_lines(text: str) -> list[str]:
    return [line for _, line in _logical_lines_with_positions(text)]


def _logical_lines_with_positions(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    buffer = ""
    start = 1
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line and not buffer:
            continue
        if not buffer:
            start = number
        buffer = f"{buffer} {line}".strip() if buffer else line
        if buffer.endswith("\\"):
            buffer = buffer[:-1].rstrip()
            continue
        lines.append((start, buffer))
        buffer = ""
    if buffer:
        lines.append((start, buffer))
    return lines


def _extract_requirements(text: str, kind: str) -> _Extracted:
    versions_by_name: dict[str, list[str]] = {}
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

    versions = {name: values for name, values in versions_by_name.items() if name not in ignored}
    return _Extracted(versions=versions, ignored=ignored, warnings=warnings)


def _clean_requirement_line(logical_line: str) -> str | None:
    line = logical_line.split("#", 1)[0].strip()
    line = _HASH_RE.sub("", line).strip()
    if not line or line.startswith("-") or _URL_REQUIREMENT_RE.search(line):
        return None
    return line


def _pin_from_requirement(
    line: str,
    kind: str,
    *,
    file: str | None,
    line_number: int,
) -> DependencyPin | None:
    try:
        requirement = Requirement(line)
    except InvalidRequirement:
        return None
    specifiers = list(requirement.specifier)
    version: str | None = None
    if len(specifiers) == 1:
        specifier = specifiers[0]
        if specifier.operator == "==" and not specifier.version.endswith(".*"):
            version = specifier.version
    return DependencyPin(
        package=requirement.name,
        version=version,
        source=kind,
        specifier=str(requirement.specifier) if requirement.specifier else None,
        raw=line,
        extras=tuple(requirement.extras),
        marker=str(requirement.marker) if requirement.marker is not None else None,
        file=file,
        line=line_number,
    )


def _pins_from_requirements(text: str, kind: str, file: str | None) -> list[DependencyPin]:
    pins: list[DependencyPin] = []
    for line_number, logical_line in _logical_lines_with_positions(text):
        line = _clean_requirement_line(logical_line)
        if line is None:
            continue
        pin = _pin_from_requirement(line, kind, file=file, line_number=line_number)
        if pin is not None:
            pins.append(pin)
    return pins


def _pins_from_toml(text: str, kind: str, file: str | None) -> list[DependencyPin]:
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if _PACKAGE_TABLE_RE.match(line)]
    pins: list[DependencyPin] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        name: str | None = None
        version: str | None = None
        version_line: int | None = None
        for offset in range(start + 1, end):
            line = lines[offset]
            if name is None and (match := _TOML_NAME_RE.match(line)) is not None:
                name = match.group("value")
            if version is None and (match := _TOML_VERSION_RE.match(line)) is not None:
                version = match.group("value")
                version_line = offset + 1
        if name is None or version is None or version_line is None:
            continue
        try:
            pins.append(
                DependencyPin(
                    package=name,
                    version=version,
                    source=kind,
                    specifier=f"=={version}",
                    raw=f"{name}=={version}",
                    file=file,
                    line=version_line,
                )
            )
        except ValueError:
            continue
    return pins


def extract_dependency_pins(
    text: str,
    kind: str,
    *,
    file: str | None = None,
) -> tuple[DependencyPin, ...]:
    """Extract pinned dependencies with source locations for dependency-constraint checks."""
    extractor = _TOML_PIN_EXTRACTORS.get(kind)
    if extractor is None:
        extractor = _pins_from_requirements
    return tuple(extractor(_normalize_text(text), kind, file))


_EXTRACTORS = {
    "uv.lock": _extract_toml,
    "poetry.lock": _extract_toml,
    "requirements": _extract_requirements,
}
_TomlPinExtractor = Callable[[str, str, str | None], list[DependencyPin]]
_TOML_PIN_EXTRACTORS: dict[str, _TomlPinExtractor] = {
    "uv.lock": _pins_from_toml,
    "poetry.lock": _pins_from_toml,
}


def extract_pinned_versions(text: str, kind: str) -> dict[str, list[str]]:
    """Return pinned versions by canonical package name using the lockfile parser."""
    extractor = _EXTRACTORS.get(kind)
    if extractor is None:
        supported = ", ".join(SUPPORTED_KINDS)
        raise LockParseError(f"Unknown lockfile kind {kind!r}; expected one of: {supported}")
    return dict(extractor(_normalize_text(text), kind).versions)


def parse_lock_pair(
    old_text: str,
    new_text: str,
    kind: str,
    *,
    new_lock_path: Path | str | None = None,
) -> ParseResult:
    """Compare two lockfile contents and return the changed packages."""
    extractor = _EXTRACTORS.get(kind)
    if extractor is None:
        supported = ", ".join(SUPPORTED_KINDS)
        raise LockParseError(f"Unknown lockfile kind {kind!r}; expected one of: {supported}")

    old = extractor(_normalize_text(old_text), kind)
    new = extractor(_normalize_text(new_text), kind)

    ignored = old.ignored | new.ignored

    bumps: list[Bump] = []
    unresolved: list[Unresolved] = []
    for name in sorted(set(old.versions) | set(new.versions)):
        if name in ignored:
            continue
        old_raw = old.versions.get(name, [])
        new_raw = new.versions.get(name, [])
        old_versions = {Version(value) for value in old_raw}
        new_versions = {Version(value) for value in new_raw}
        if len(old_versions) > 1 or len(new_versions) > 1:
            if old_versions == new_versions:
                continue
            unresolved.append(
                Unresolved(
                    package=name,
                    removed=[value for value in old_raw if Version(value) not in new_versions],
                    added=[value for value in new_raw if Version(value) not in old_versions],
                )
            )
            continue
        old_version = old_raw[0] if old_raw else None
        new_version = new_raw[0] if new_raw else None
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

    unresolved_warnings = [
        f"Package '{item.package}' has multiple versions in {kind}; recording it as unresolved"
        for item in unresolved
    ]
    warnings = list(dict.fromkeys(old.warnings + new.warnings + unresolved_warnings))
    pin_file = str(new_lock_path) if new_lock_path is not None else None
    pins = extract_dependency_pins(new_text, kind, file=pin_file)
    return ParseResult(bumps=bumps, warnings=warnings, unresolved=unresolved, new_pins=pins)
