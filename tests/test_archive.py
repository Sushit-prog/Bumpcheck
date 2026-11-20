from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from bumpcheck import archive


def make_wheel(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as handle:
        for name, data in entries.items():
            handle.writestr(name, data)
    return buffer.getvalue()


def make_tar(entries: dict[str, bytes], *, root: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
        for name, data in entries.items():
            path = f"{root}/{name}" if root else name
            info = tarfile.TarInfo(path)
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_zip_extracts_only_wheel_sources(tmp_path: Path) -> None:
    data = make_wheel(
        {
            "pkg/__init__.py": b"value = 1\n",
            "pkg/types.pyi": b"value: int\n",
            "pkg/py.typed": b"",
            "pkg-1.0.dist-info/METADATA": b"Name: pkg\n",
            "pkg-1.0.dist-info/RECORD": b"pkg/__init__.py,,\n",
            "pkg-1.0.dist-info/top_level.txt": b"pkg\n",
            "docs/README.md": b"ignored\n",
        }
    )

    stats = archive.extract_zip(data, tmp_path / "wheel", cap_bytes=1024 * 1024)

    extracted = {
        path.relative_to(tmp_path / "wheel").as_posix()
        for path in (tmp_path / "wheel").rglob("*")
        if path.is_file()
    }
    assert extracted == {
        "pkg/__init__.py",
        "pkg/types.pyi",
        "pkg/py.typed",
        "pkg-1.0.dist-info/METADATA",
    }
    assert stats.files_extracted == 4
    assert stats.has_skipped_python is False


def test_tar_selective_extract_skips_invalid_names_and_case_collisions(tmp_path: Path) -> None:
    data = make_tar(
        {
            "main.py": b"value = 1\n",
            "Main.py": b"value = 2\n",
            "bad:name.py": b"value = 3\n",
            "README.md": b"ignored\n",
            ".github/workflows/ci.yml": b"name: ci\n",
            "pyproject.toml": b"[project]\n",
        },
        root="repo-sha",
    )

    stats = archive.extract_tar(
        data,
        tmp_path / "repo",
        selector=archive.select_repo_member,
        cap_bytes=1024 * 1024,
        strip_root=True,
    )

    extracted = {
        path.relative_to(tmp_path / "repo").as_posix()
        for path in (tmp_path / "repo").rglob("*")
        if path.is_file()
    }
    assert extracted == {"main.py", ".github/workflows/ci.yml", "pyproject.toml"}
    reasons = {item.reason for item in stats.skipped}
    assert reasons == {"case_collision", "windows_invalid_name"}
    assert stats.has_skipped_python is True
    assert not (tmp_path / "repo").joinpath("repo-sha").exists()


def test_declared_tar_size_is_checked_before_extraction(tmp_path: Path) -> None:
    data = make_tar({"main.py": b"x = 1\n"}, root="repo-sha")

    with pytest.raises(archive.ArchiveError) as excinfo:
        archive.extract_tar(
            data,
            tmp_path / "repo",
            selector=archive.select_repo_member,
            cap_bytes=2,
            strip_root=True,
        )

    assert excinfo.value.kind == "too_large"
    assert not (tmp_path / "repo").exists()


def test_tar_rejects_symlinks(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
        link = tarfile.TarInfo("repo-sha/evil.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside.py"
        handle.addfile(link)
    data = buffer.getvalue()

    with pytest.raises(archive.ArchiveError) as excinfo:
        archive.extract_tar(
            data,
            tmp_path / "repo",
            selector=archive.select_repo_member,
            cap_bytes=1024,
            strip_root=True,
        )

    assert excinfo.value.kind == "unsafe_archive"


def test_selective_wheel_extraction_keeps_top_level_from_sources(tmp_path: Path) -> None:
    from bumpcheck.apidiff import top_level_modules

    data = make_wheel({"pkg/__init__.py": b"def foo():\n    return 1\n"})
    archive.extract_zip(
        data,
        tmp_path / "wheel",
        selector=archive.select_wheel_member,
        cap_bytes=1024,
    )

    assert top_level_modules(tmp_path / "wheel") == ["pkg"]
