"""Static API diff between two versions of a PyPI package, using Griffe."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import griffe
from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["high", "medium", "low"]
Status = Literal["ok", "no_api_diff"]
Reason = Literal[
    "no_wheel",
    "no_python_sources",
    "no_top_level",
    "download_failed",
    "hash_mismatch",
    "unsafe_archive",
    "too_large",
    "unpack_failed",
    "unparsable_sources",
    "griffe_error",
]

Fetcher = Callable[[str], bytes]

_PYPI_JSON_URL = "https://pypi.org/pypi/{name}/{version}/json"
_USER_AGENT = "bumpcheck"
_TIMEOUT_SECONDS = 30
_RETRIES = 3
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_TOTAL_BYTES = 200 * 1024 * 1024
_MAX_MEMBERS = 20000
_CHUNK_SIZE = 64 * 1024
_WHEEL_MARKER = ".bumpcheck-wheel"

_SEVERITY: dict[str, Severity] = {
    "OBJECT_REMOVED": "high",
    "PARAMETER_REMOVED": "high",
    "PARAMETER_ADDED_REQUIRED": "high",
    "PARAMETER_CHANGED_KIND": "high",
    "PARAMETER_CHANGED_REQUIRED": "high",
    "PARAMETER_MOVED": "high",
    "OBJECT_CHANGED_KIND": "high",
    "PARAMETER_CHANGED_DEFAULT": "medium",
    "RETURN_CHANGED_TYPE": "medium",
    "CLASS_REMOVED_BASE": "medium",
    "ATTRIBUTE_CHANGED_TYPE": "low",
    "ATTRIBUTE_CHANGED_VALUE": "low",
}

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


class _ApiDiffError(Exception):
    """An internal error that maps to a ``no_api_diff`` result and a reason."""

    def __init__(self, reason: Reason, message: str = "") -> None:
        super().__init__(message)
        self.reason: Reason = reason
        self.message = message


class Location(BaseModel):
    """A source location: a file path and a line number."""

    model_config = ConfigDict(frozen=True)

    file: str
    line: int


class ApiChange(BaseModel):
    """A single breaking API change between two versions."""

    model_config = ConfigDict(frozen=True)

    kind: str
    symbol: str
    severity: Severity
    detail: str
    parameter: str | None = None
    old_location: Location | None = None
    new_location: Location | None = None


class ApiDiffResult(BaseModel):
    """The outcome of comparing the API of two package versions."""

    model_config = ConfigDict(frozen=True)

    package: str
    old_version: str
    new_version: str
    status: Status
    reason: Reason | None = None
    api_definition: str = "griffe-default"
    changes: list[ApiChange] = Field(default_factory=list)
    top_level: list[str] = Field(default_factory=list)
    compared: list[str] = Field(default_factory=list)
    old_wheel: str | None = None
    new_wheel: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("package")
    @classmethod
    def _canonicalize_package(cls, value: str) -> str:
        return canonicalize_name(value)

    @field_validator("changes")
    @classmethod
    def _sort_changes(cls, value: list[ApiChange]) -> list[ApiChange]:
        return sorted(
            value,
            key=lambda change: (
                _SEVERITY_RANK[change.severity],
                change.symbol,
                change.kind,
                change.parameter or "",
                change.detail,
            ),
        )

    @field_validator("top_level", "compared")
    @classmethod
    def _sort_names(cls, value: list[str]) -> list[str]:
        return sorted(value)


def _default_fetch(url: str) -> bytes:
    """Fetch a URL with retries and exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in _RETRYABLE_STATUS:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt + 1 < _RETRIES:
            time.sleep(2**attempt)
    raise _ApiDiffError("download_failed", f"Could not download {url}: {last_error}")


def _resolve_cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is not None:
        base = Path(cache_dir)
    elif os.environ.get("BUMPCHECK_CACHE"):
        base = Path(os.environ["BUMPCHECK_CACHE"])
    else:
        base = Path.home() / ".cache" / "bumpcheck"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fetch_bytes(fetcher: Fetcher, url: str) -> bytes:
    try:
        return fetcher(url)
    except _ApiDiffError:
        raise
    except Exception as exc:
        raise _ApiDiffError("download_failed", f"{type(exc).__name__}: {exc}") from exc


def _parse_metadata(raw: bytes, name: str, version: str) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _ApiDiffError(
            "download_failed", f"Invalid metadata for {name} {version}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise _ApiDiffError("download_failed", f"Unexpected metadata for {name} {version}")
    return data


def _choose_wheel(urls: list, name: str, version: str) -> dict:
    wheels = [
        entry
        for entry in urls
        if isinstance(entry, dict)
        and isinstance(entry.get("filename"), str)
        and entry["filename"].endswith(".whl")
        and entry.get("packagetype", "bdist_wheel") == "bdist_wheel"
    ]
    if not wheels:
        raise _ApiDiffError("no_wheel", f"No wheel available for {name} {version}")
    wheels.sort(
        key=lambda entry: (0 if "py3-none-any" in entry["filename"] else 1, entry["filename"])
    )
    return wheels[0]


def _is_unsafe(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        return True
    parts = Path(name).parts
    return any(part == ".." or ":" in part for part in parts)


def _is_symlink(member: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(member.external_attr >> 16)


def _extract_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    destination: Path,
    limit: int,
) -> int:
    if member.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with archive.open(member) as source, destination.open("wb") as target:
        while True:
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise _ApiDiffError("too_large", f"Archive exceeds {_MAX_TOTAL_BYTES} bytes")
            target.write(chunk)
    return written


def _unpack(wheel_bytes: bytes, target: Path, parent: Path, wheel_filename: str) -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=parent, prefix=f"{target.name}-"))
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            members = archive.infolist()
            if len(members) > _MAX_MEMBERS:
                raise _ApiDiffError("too_large", f"Archive has more than {_MAX_MEMBERS} members")
            total = 0
            for member in members:
                if _is_unsafe(member.filename) or _is_symlink(member):
                    raise _ApiDiffError("unsafe_archive", f"Unsafe member: {member.filename}")
                destination = temp_dir / member.filename
                total += _extract_member(archive, member, destination, _MAX_TOTAL_BYTES - total)
        (temp_dir / _WHEEL_MARKER).write_text(wheel_filename, encoding="utf-8")
        temp_dir.rename(target)
    except _ApiDiffError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise _ApiDiffError("unpack_failed", f"{type(exc).__name__}: {exc}") from exc


def get_unpacked(
    name: str,
    version: str,
    cache_dir: Path,
    fetcher: Fetcher,
    warnings: list[str] | None = None,
) -> Path:
    """Download and unpack the wheel for ``name`` ``version``, returning its directory."""
    target = Path(cache_dir) / canonicalize_name(name) / version
    if target.is_dir() and any(target.iterdir()):
        return target

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    metadata_url = _PYPI_JSON_URL.format(name=name, version=version)
    data = _parse_metadata(_fetch_bytes(fetcher, metadata_url), name, version)
    if warnings is not None and data.get("info", {}).get("yanked"):
        warnings.append(f"Version {version} of {name} is marked as yanked on PyPI")

    entry = _choose_wheel(data.get("urls") or [], name, version)
    wheel_bytes = _fetch_bytes(fetcher, entry["url"])
    expected = (entry.get("digests") or {}).get("sha256")
    if expected is None:
        if warnings is not None:
            warnings.append(f"No sha256 digest available for {entry['filename']}")
    elif hashlib.sha256(wheel_bytes).hexdigest() != expected:
        raise _ApiDiffError("hash_mismatch", f"sha256 mismatch for {entry['filename']}")

    _unpack(wheel_bytes, target, parent, entry["filename"])
    return target


def _read_wheel_name(unpacked_dir: Path) -> str | None:
    marker = unpacked_dir / _WHEEL_MARKER
    if marker.is_file():
        return marker.read_text(encoding="utf-8").strip() or None
    return None


def _find_dist_info(unpacked_dir: Path) -> Path | None:
    for entry in sorted(unpacked_dir.iterdir()):
        if entry.is_dir() and entry.name.endswith(".dist-info"):
            return entry
    return None


def _names_from_record(record: Path, dist_info_name: str) -> list[str]:
    names: list[str] = []
    for line in record.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        path = line.split(",", 1)[0].strip().replace("\\", "/")
        if not path:
            continue
        first = path.split("/", 1)[0]
        if not first or first == dist_info_name:
            continue
        if first.endswith(".dist-info") or first.endswith(".data"):
            continue
        if first.endswith(".py"):
            first = first[:-3]
        names.append(first)
    return names


def top_level_modules(unpacked_dir: Path) -> list[str]:
    """Return the top-level import names shipped in an unpacked wheel."""
    dist_info = _find_dist_info(unpacked_dir)
    if dist_info is None:
        return []
    top_level = dist_info / "top_level.txt"
    if top_level.is_file():
        names = [line.strip() for line in top_level.read_text(encoding="utf-8").splitlines()]
    else:
        record = dist_info / "RECORD"
        names = _names_from_record(record, dist_info.name) if record.is_file() else []
    return sorted({name for name in names if name})


def _has_python(unpacked_dir: Path) -> bool:
    return any(unpacked_dir.rglob("*.py"))


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


def _python_files(base: Path, name: str) -> list[Path]:
    single = base / f"{name}.py"
    if single.is_file():
        return [single]
    package = base / name
    if package.is_dir():
        return sorted(package.rglob("*.py"))
    return []


_UNPARSABLE_ERRORS = (SyntaxError, ValueError, UnicodeDecodeError, RecursionError)


def _parse_error_message(exc: Exception) -> str:
    if isinstance(exc, SyntaxError):
        return f"{exc.msg} (line {exc.lineno})"
    return f"{type(exc).__name__}: {exc}"


def _syntax_failures(old_dir: Path, new_dir: Path, names: list[str]) -> list[str]:
    failures: list[str] = []
    for base in (old_dir, new_dir):
        for name in names:
            for path in _python_files(base, name):
                try:
                    ast.parse(path.read_bytes(), filename=str(path))
                except _UNPARSABLE_ERRORS as exc:
                    failures.append(f"{_relative(path, base)}: {_parse_error_message(exc)}")
    return failures


def _location(obj: object, base: Path, line: int | None = None) -> Location | None:
    if getattr(obj, "is_alias", False):
        filepath = getattr(getattr(obj, "parent", None), "filepath", None)
    else:
        filepath = getattr(obj, "filepath", None)
    if filepath is None:
        return None
    if line is None:
        line = getattr(obj, "lineno", None) or 0
    return Location(file=_relative(Path(filepath), base), line=line)


_PARAMETER_KINDS = frozenset(
    {
        "PARAMETER_REMOVED",
        "PARAMETER_ADDED_REQUIRED",
        "PARAMETER_CHANGED_KIND",
        "PARAMETER_MOVED",
        "PARAMETER_CHANGED_DEFAULT",
        "PARAMETER_CHANGED_REQUIRED",
    }
)


def _parameter_name(breakage: griffe.Breakage) -> str | None:
    for value in (breakage.old_value, breakage.new_value):
        name = getattr(value, "name", None)
        if isinstance(name, str):
            return name
    return None


def _resolve_old_location(
    symbol_path: str, old_root: object, old_dir: Path, parameter: str | None = None
) -> Location | None:
    try:
        parts = symbol_path.split(".")
        obj = old_root
        if getattr(obj, "path", None) != parts[0]:
            return None
        for part in parts[1:]:
            obj = obj.get_member(part)
        if getattr(obj, "is_alias", False):
            return None
        line = getattr(obj, "lineno", None)
        if parameter is not None:
            resolved = getattr(obj, "parameters", None)
            if resolved is not None:
                try:
                    param = resolved[parameter]
                except (KeyError, TypeError):
                    param = None
                param_line = getattr(param, "lineno", None)
                if param_line is not None:
                    line = param_line
        return _location(obj, old_dir, line)
    except Exception:
        return None


def _breakage_to_change(
    breakage: griffe.Breakage,
    old_root: object,
    old_dir: Path,
    new_dir: Path,
    warnings: list[str],
) -> ApiChange:
    kind = breakage.kind.name
    severity = _SEVERITY.get(kind)
    if severity is None:
        severity = "medium"
        warnings.append(f"Unmapped breakage kind {kind}; treating it as medium severity")
    obj = breakage.obj
    parameter = _parameter_name(breakage) if kind in _PARAMETER_KINDS else None
    if kind == "OBJECT_REMOVED":
        old_location = _location(obj, old_dir)
        new_location = None
    else:
        old_location = _resolve_old_location(obj.path, old_root, old_dir, parameter)
        new_location = _location(obj, new_dir)
    return ApiChange(
        kind=kind,
        symbol=obj.path,
        severity=severity,
        detail=breakage.explain(griffe.ExplanationStyle.MARKDOWN),
        parameter=parameter,
        old_location=old_location,
        new_location=new_location,
    )


def _load_versions(old_dir: Path, new_dir: Path, names: list[str]) -> dict[str, tuple]:
    loaded: dict[str, tuple] = {}
    for name in names:
        old_obj = griffe.load(
            name,
            search_paths=[str(old_dir)],
            try_relative_path=False,
            allow_inspection=False,
        )
        new_obj = griffe.load(
            name,
            search_paths=[str(new_dir)],
            try_relative_path=False,
            allow_inspection=False,
        )
        loaded[name] = (old_obj, new_obj)
    return loaded


def _failure(
    package: str,
    old_version: str,
    new_version: str,
    error: _ApiDiffError,
    *,
    warnings: list[str],
    old_wheel: str | None = None,
    new_wheel: str | None = None,
    top_level: list[str] | None = None,
    compared: list[str] | None = None,
) -> ApiDiffResult:
    extra = [f"{error.reason}: {error.message}"] if error.message else []
    return ApiDiffResult(
        package=package,
        old_version=old_version,
        new_version=new_version,
        status="no_api_diff",
        reason=error.reason,
        changes=[],
        top_level=top_level or [],
        compared=compared or [],
        old_wheel=old_wheel,
        new_wheel=new_wheel,
        warnings=[*warnings, *extra],
    )


def diff_versions(
    name: str,
    old_version: str,
    new_version: str,
    cache_dir: Path | None = None,
    fetcher: Fetcher | None = None,
) -> ApiDiffResult:
    """Compare the public API of two versions of a PyPI package."""
    fetcher = fetcher or _default_fetch
    cache = _resolve_cache_dir(cache_dir)
    package = canonicalize_name(name)
    warnings: list[str] = []

    try:
        old_dir = get_unpacked(name, old_version, cache, fetcher, warnings)
    except _ApiDiffError as exc:
        return _failure(package, old_version, new_version, exc, warnings=warnings)
    old_wheel = _read_wheel_name(old_dir)

    try:
        new_dir = get_unpacked(name, new_version, cache, fetcher, warnings)
    except _ApiDiffError as exc:
        return _failure(
            package, old_version, new_version, exc, warnings=warnings, old_wheel=old_wheel
        )
    new_wheel = _read_wheel_name(new_dir)

    if not _has_python(old_dir) or not _has_python(new_dir):
        error = _ApiDiffError("no_python_sources", "No Python sources in the chosen wheel")
        return _failure(
            package,
            old_version,
            new_version,
            error,
            warnings=warnings,
            old_wheel=old_wheel,
            new_wheel=new_wheel,
        )

    old_top = top_level_modules(old_dir)
    new_top = top_level_modules(new_dir)
    top_level = sorted(set(old_top) | set(new_top))
    compared = sorted(set(old_top) & set(new_top))
    if not top_level:
        error = _ApiDiffError("no_top_level", "Could not determine any top-level module")
        return _failure(
            package,
            old_version,
            new_version,
            error,
            warnings=warnings,
            old_wheel=old_wheel,
            new_wheel=new_wheel,
            top_level=top_level,
            compared=compared,
        )

    try:
        failures = _syntax_failures(old_dir, new_dir, compared)
    except Exception as exc:
        error = _ApiDiffError("griffe_error", "")
        return _failure(
            package,
            old_version,
            new_version,
            error,
            warnings=[*warnings, f"{type(exc).__name__}: {exc}"],
            old_wheel=old_wheel,
            new_wheel=new_wheel,
            top_level=top_level,
            compared=compared,
        )
    if failures:
        error = _ApiDiffError("unparsable_sources", "Some sources could not be parsed")
        return _failure(
            package,
            old_version,
            new_version,
            error,
            warnings=[*warnings, *failures[:5]],
            old_wheel=old_wheel,
            new_wheel=new_wheel,
            top_level=top_level,
            compared=compared,
        )

    try:
        loaded = _load_versions(old_dir, new_dir, compared)
    except Exception as exc:
        error = _ApiDiffError("griffe_error", "")
        return _failure(
            package,
            old_version,
            new_version,
            error,
            warnings=[*warnings, f"{type(exc).__name__}: {exc}"],
            old_wheel=old_wheel,
            new_wheel=new_wheel,
            top_level=top_level,
            compared=compared,
        )

    changes: list[ApiChange] = []
    try:
        for top in compared:
            old_obj, new_obj = loaded[top]
            for breakage in griffe.find_breaking_changes(old_obj, new_obj):
                changes.append(_breakage_to_change(breakage, old_obj, old_dir, new_dir, warnings))
    except Exception as exc:
        error = _ApiDiffError("griffe_error", "")
        return _failure(
            package,
            old_version,
            new_version,
            error,
            warnings=[*warnings, f"{type(exc).__name__}: {exc}"],
            old_wheel=old_wheel,
            new_wheel=new_wheel,
            top_level=top_level,
            compared=compared,
        )

    for top in old_top:
        if top not in new_top:
            changes.append(
                ApiChange(
                    kind="TopLevelRemoved",
                    symbol=top,
                    severity="high",
                    detail=f"Top-level module {top!r} was removed",
                )
            )

    return ApiDiffResult(
        package=package,
        old_version=old_version,
        new_version=new_version,
        status="ok",
        changes=changes,
        top_level=top_level,
        compared=compared,
        old_wheel=old_wheel,
        new_wheel=new_wheel,
        warnings=warnings,
    )
