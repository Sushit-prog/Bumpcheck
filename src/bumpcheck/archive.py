"""Safe, selective extraction helpers for untrusted archives."""

from __future__ import annotations

import io
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

ArchiveErrorKind = Literal["unsafe_archive", "too_large", "unpack_failed"]

MAX_EXTRACTED_BYTES = 50 * 1024 * 1024

_WHEEL_SUFFIXES = frozenset({".py", ".pyi"})
_WHEEL_NAMES = frozenset({"py.typed", "METADATA"})
_REPO_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "noxfile.py",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
    }
)
_WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
_WINDOWS_INVALID = frozenset({"<", ">", ":", '"', "?", "*", "|"})


class ArchiveError(Exception):
    """An archive could not be extracted safely."""

    def __init__(self, kind: ArchiveErrorKind, message: str = "") -> None:
        self.kind = kind
        super().__init__(message or kind)


@dataclass(frozen=True)
class SkippedFile:
    path: str
    size: int
    reason: str


@dataclass
class ExtractionStats:
    files_extracted: int = 0
    bytes_extracted: int = 0
    skipped: list[SkippedFile] = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def skipped_bytes(self) -> int:
        return sum(item.size for item in self.skipped)

    @property
    def has_skipped_python(self) -> bool:
        return any(item.path.endswith(".py") for item in self.skipped)

    def add_skipped(self, path: str, size: int, reason: str) -> None:
        self.skipped.append(SkippedFile(path=path, size=size, reason=reason))

    def to_dict(self) -> dict[str, object]:
        reasons: dict[str, int] = {}
        for item in self.skipped:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        return {
            "files_extracted": self.files_extracted,
            "bytes_extracted": self.bytes_extracted,
            "files_skipped": self.skipped_count,
            "bytes_skipped": self.skipped_bytes,
            "skip_reasons": reasons,
            "skipped": [item.__dict__ for item in self.skipped],
        }


Selector = Callable[[str], bool]
MemberWriter = Callable[[zipfile.ZipFile, zipfile.ZipInfo, Path, int], int]


def select_wheel_member(name: str) -> bool:
    path = PurePosixPath(name)
    if path.name in _WHEEL_NAMES:
        return True
    return path.suffix.lower() in _WHEEL_SUFFIXES


def select_repo_member(name: str) -> bool:
    path = PurePosixPath(name)
    parts = path.parts
    filename = path.name
    suffix = path.suffix.lower()
    if suffix in {".py", ".pyi"} or filename in _REPO_NAMES:
        return True
    if filename.startswith("requirements") and suffix == ".txt":
        return True
    if len(parts) >= 3 and parts[0] == ".github" and parts[1] == "workflows":
        return suffix in {".yml", ".yaml"}
    return False


def _unsafe_reason(name: str) -> str | None:
    if not name or name.startswith("/"):
        return "absolute_path"
    path = PurePosixPath(name)
    if any(part == ".." for part in path.parts):
        return "parent_traversal"
    return None


def _windows_skip_reason(name: str) -> str | None:
    if "\\" in name or re.match(r"^[A-Za-z]:", name):
        return "windows_invalid_name"
    path = PurePosixPath(name)
    for part in path.parts:
        if any(char in part for char in _WINDOWS_INVALID):
            return "windows_invalid_name"
        if part != part.rstrip(" ."):
            return "windows_invalid_name"
        if _WINDOWS_RESERVED.match(part):
            return "windows_invalid_name"
    if len(path.as_posix()) > 260 or any(len(part) > 255 for part in path.parts):
        return "path_too_long"
    return None


def _case_key(name: str) -> str:
    return PurePosixPath(name).as_posix().lower()


def _prepare_path(
    name: str, root: Path, seen: dict[str, str], stats: ExtractionStats, size: int
) -> Path | None:
    unsafe = _unsafe_reason(name)
    if unsafe is not None:
        raise ArchiveError("unsafe_archive", f"Unsafe member: {name} ({unsafe})")
    skip_reason = _windows_skip_reason(name)
    if skip_reason is not None:
        stats.add_skipped(name, size, skip_reason)
        return None
    key = _case_key(name)
    existing = seen.get(key)
    if existing is not None and existing != name:
        stats.add_skipped(name, size, "case_collision")
        return None
    seen[key] = name
    parts = PurePosixPath(name).parts
    if any(part.startswith(".") and part not in {".github"} for part in parts[:-1]):
        stats.add_skipped(name, size, "hidden_path")
        return None
    return root.joinpath(*parts)


def extract_zip_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    destination: Path,
    limit: int,
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with archive.open(member) as source, destination.open("wb") as target:
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise ArchiveError("too_large", "Archive exceeds extraction cap")
            target.write(chunk)
    return written


def extract_zip(
    data: bytes,
    target: Path,
    *,
    selector: Selector = select_wheel_member,
    cap_bytes: int,
    marker: tuple[str, str] | None = None,
    member_writer: MemberWriter | None = None,
) -> ExtractionStats:
    write_member = member_writer or extract_zip_member
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(dir=parent, prefix=f"{target.name}-"))
    stats = ExtractionStats()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            candidates = [
                member for member in members if selector(PurePosixPath(member.filename).as_posix())
            ]
            declared = sum(member.file_size for member in candidates if not member.is_dir())
            if declared > cap_bytes:
                raise ArchiveError("too_large", f"Archive declares {declared} extracted bytes")
            seen: dict[str, str] = {}
            for member in candidates:
                if member.is_dir():
                    continue
                name = PurePosixPath(member.filename).as_posix()
                symlink = stat.S_ISLNK(member.external_attr >> 16)
                mode = stat.S_IFMT(member.external_attr >> 16)
                if symlink or mode not in {0, stat.S_IFREG}:
                    raise ArchiveError("unsafe_archive", f"Unsafe member: {name}")
                destination = _prepare_path(name, temp_dir, seen, stats, member.file_size)
                if destination is None:
                    continue
                written = write_member(
                    archive, member, destination, cap_bytes - stats.bytes_extracted
                )
                stats.files_extracted += 1
                stats.bytes_extracted += written
        if marker is not None:
            marker_name, marker_text = marker
            (temp_dir / marker_name).write_text(marker_text, encoding="utf-8")
        if target.exists():
            raise ArchiveError("unpack_failed", f"Target already exists: {target}")
        temp_dir.rename(target)
    except ArchiveError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile, ValueError, RuntimeError) as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ArchiveError("unpack_failed", f"{type(exc).__name__}: {exc}") from exc
    return stats


def _strip_root(members: list[tarfile.TarInfo]) -> list[tuple[str, tarfile.TarInfo]]:
    names = [PurePosixPath(member.name).as_posix() for member in members]
    roots = {name.split("/", 1)[0] for name in names if name and not name.startswith("/")}
    if len(roots) != 1:
        return [(name, member) for name, member in zip(names, members, strict=True)]
    root = next(iter(roots))
    stripped: list[tuple[str, tarfile.TarInfo]] = []
    for name, member in zip(names, members, strict=True):
        if name == root:
            stripped.append(("", member))
        elif name.startswith(f"{root}/"):
            stripped.append((name[len(root) + 1 :], member))
        else:
            stripped.append((name, member))
    return stripped


def extract_tar(
    data: bytes,
    target: Path,
    *,
    selector: Selector = select_repo_member,
    cap_bytes: int,
    strip_root: bool = False,
) -> ExtractionStats:
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(dir=parent, prefix=f"{target.name}-"))
    stats = ExtractionStats()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            selected = (
                _strip_root(members)
                if strip_root
                else [(PurePosixPath(member.name).as_posix(), member) for member in members]
            )
            candidates = [(name, member) for name, member in selected if name and selector(name)]
            declared = sum(member.size for _, member in candidates if member.isfile())
            if declared > cap_bytes:
                raise ArchiveError("too_large", f"Archive declares {declared} extracted bytes")
            seen: dict[str, str] = {}
            for name, member in candidates:
                if not member.isfile():
                    raise ArchiveError("unsafe_archive", f"Unsafe member: {name}")
                destination = _prepare_path(name, temp_dir, seen, stats, member.size)
                if destination is None:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ArchiveError("unpack_failed", f"Could not read member: {name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with source, destination.open("wb") as handle:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > cap_bytes - stats.bytes_extracted:
                            raise ArchiveError("too_large", "Archive exceeds extraction cap")
                        handle.write(chunk)
                stats.files_extracted += 1
                stats.bytes_extracted += written
        if target.exists():
            raise ArchiveError("unpack_failed", f"Target already exists: {target}")
        temp_dir.rename(target)
    except ArchiveError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except (OSError, tarfile.TarError, ValueError, RuntimeError) as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ArchiveError("unpack_failed", f"{type(exc).__name__}: {exc}") from exc
    return stats


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())
