from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import griffe
from typer.testing import CliRunner

from bumpcheck import apidiff
from bumpcheck.apidiff import ApiDiffResult, diff_versions
from bumpcheck.cli import app

PYPI = "https://pypi.org/pypi"
FILES = "https://files.pythonhosted.org"


class FakeFetcher:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"Unexpected URL: {url}")
        return self.responses[url]


def make_wheel(
    version: str,
    sources: dict[str, str | bytes],
    *,
    name: str = "pkg",
    top_level: tuple[str, ...] | None = ("pkg",),
    record: bool = False,
) -> bytes:
    buffer = io.BytesIO()
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    entries: dict[str, str | bytes] = dict(sources)
    entries[f"{dist_info}/METADATA"] = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    if top_level is not None:
        entries[f"{dist_info}/top_level.txt"] = "\n".join(top_level) + "\n"
    if record:
        lines = [f"{path},," for path in entries]
        lines.append(f"{dist_info}/RECORD,,")
        entries[f"{dist_info}/RECORD"] = "\n".join(lines) + "\n"
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def wheel_filename(name: str, version: str) -> str:
    return f"{name.replace('-', '_')}-{version}-py3-none-any.whl"


def metadata(
    name: str,
    version: str,
    filename: str,
    wheel: bytes,
    *,
    yanked: bool = False,
    requires_python: str | None = None,
) -> bytes:
    info: dict[str, object] = {"name": name, "version": version, "yanked": yanked}
    if requires_python is not None:
        info["requires_python"] = requires_python
    return json.dumps(
        {
            "info": info,
            "urls": [
                {
                    "filename": filename,
                    "url": f"{FILES}/{filename}",
                    "packagetype": "bdist_wheel",
                    "digests": {"sha256": hashlib.sha256(wheel).hexdigest()},
                }
            ],
        }
    ).encode("utf-8")


def responses_for(
    name: str,
    version: str,
    wheel: bytes,
    *,
    yanked: bool = False,
    requires_python: str | None = None,
) -> dict[str, bytes]:
    filename = wheel_filename(name, version)
    return {
        f"{PYPI}/{name}/{version}/json": metadata(
            name,
            version,
            filename,
            wheel,
            yanked=yanked,
            requires_python=requires_python,
        ),
        f"{FILES}/{filename}": wheel,
    }


def diff_of(
    tmp_path: Path,
    old_sources: dict[str, str | bytes],
    new_sources: dict[str, str | bytes],
    *,
    name: str = "pkg",
    old: str = "1.0.0",
    new: str = "2.0.0",
    top_level: tuple[str, ...] | None = ("pkg",),
    record: bool = False,
) -> ApiDiffResult:
    old_wheel = make_wheel(old, old_sources, name=name, top_level=top_level, record=record)
    new_wheel = make_wheel(new, new_sources, name=name, top_level=top_level, record=record)
    fetcher = FakeFetcher(
        {**responses_for(name, old, old_wheel), **responses_for(name, new, new_wheel)}
    )
    return diff_versions(name, old, new, cache_dir=tmp_path / "cache", fetcher=fetcher)


def shrink_central_uncompressed_size(data: bytes, member: str, new_size: int) -> bytes:
    buffer = bytearray(data)
    encoded = member.encode("utf-8")
    offset = 0
    while True:
        offset = buffer.find(b"PK\x01\x02", offset)
        if offset == -1:
            raise AssertionError(f"central directory entry not found: {member}")
        name_length = int.from_bytes(buffer[offset + 28 : offset + 30], "little")
        name = bytes(buffer[offset + 46 : offset + 46 + name_length])
        if name == encoded:
            buffer[offset + 24 : offset + 28] = new_size.to_bytes(4, "little")
            return bytes(buffer)
        offset += 4


def test_unchanged(tmp_path: Path) -> None:
    sources = {"pkg/__init__.py": "def foo(a, b=1):\n    return a + b\n"}

    result = diff_of(tmp_path, sources, sources)

    assert result.status == "ok"
    assert result.changes == []
    assert result.top_level == ["pkg"]
    assert result.compared == ["pkg"]
    assert result.old_wheel is not None
    assert result.new_wheel is not None
    assert result.warnings == []


def test_removed_function(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo():\n    return 1\n\ndef keep():\n    return 2\n"}
    new = {"pkg/__init__.py": "def keep():\n    return 2\n"}

    result = diff_of(tmp_path, old, new)

    assert result.status == "ok"
    assert [(c.kind, c.symbol, c.severity) for c in result.changes] == [
        ("OBJECT_REMOVED", "pkg.foo", "high")
    ]
    assert result.changes[0].old_location is not None
    assert result.changes[0].old_location.file == "pkg/__init__.py"
    assert result.changes[0].parameter is None


def test_removed_parameter(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo(name, punct):\n    return name\n"}
    new = {"pkg/__init__.py": "def foo(name):\n    return name\n"}

    result = diff_of(tmp_path, old, new)

    assert [(c.kind, c.symbol, c.severity) for c in result.changes] == [
        ("PARAMETER_REMOVED", "pkg.foo", "high")
    ]
    change = result.changes[0]
    assert change.parameter == "punct"
    assert change.old_location is not None
    assert change.old_location.file == "pkg/__init__.py"
    assert change.old_location.line == 1


def test_required_parameter_added(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo(a):\n    return a\n"}
    new = {"pkg/__init__.py": "def foo(a, b):\n    return a + b\n"}

    result = diff_of(tmp_path, old, new)

    assert [(c.kind, c.severity) for c in result.changes] == [("PARAMETER_ADDED_REQUIRED", "high")]
    assert result.changes[0].parameter == "b"


def test_moved_parameter_reports_name(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo(a, b):\n    return a\n"}
    new = {"pkg/__init__.py": "def foo(b, a):\n    return a\n"}

    result = diff_of(tmp_path, old, new)

    assert [(c.kind, c.parameter) for c in result.changes] == [
        ("PARAMETER_MOVED", "a"),
        ("PARAMETER_MOVED", "b"),
    ]
    assert all(change.old_location is not None for change in result.changes)


def test_kind_changed_parameter_reports_name(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo(name, /):\n    return name\n"}
    new = {"pkg/__init__.py": "def foo(*, name):\n    return name\n"}

    result = diff_of(tmp_path, old, new)

    assert [(c.kind, c.parameter) for c in result.changes] == [("PARAMETER_CHANGED_KIND", "name")]


def test_changed_required_parameter_reports_name(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo(a=1):\n    return a\n"}
    new = {"pkg/__init__.py": "def foo(a):\n    return a\n"}

    result = diff_of(tmp_path, old, new)

    assert [(c.kind, c.parameter) for c in result.changes] == [("PARAMETER_CHANGED_REQUIRED", "a")]


def test_optional_parameter_added_is_not_breaking(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo(a):\n    return a\n"}
    new = {"pkg/__init__.py": "def foo(a, b=1):\n    return a + b\n"}

    result = diff_of(tmp_path, old, new)

    assert result.status == "ok"
    assert result.changes == []


def test_default_value_changed(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo(a=1):\n    return a\n"}
    new = {"pkg/__init__.py": "def foo(a=2):\n    return a\n"}

    result = diff_of(tmp_path, old, new)

    assert [(c.kind, c.severity) for c in result.changes] == [
        ("PARAMETER_CHANGED_DEFAULT", "medium")
    ]
    assert result.changes[0].parameter == "a"


def test_old_location_resolution_failure_is_none(tmp_path: Path) -> None:
    root = tmp_path / "old" / "pkg"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("def foo(a, b):\n    return a\n", encoding="utf-8")
    old_root = griffe.load(
        "pkg",
        search_paths=[str(tmp_path / "old")],
        try_relative_path=False,
        allow_inspection=False,
    )

    assert apidiff._resolve_old_location("pkg.missing", old_root, tmp_path / "old") is None
    assert apidiff._resolve_old_location("other.foo", old_root, tmp_path / "old") is None
    assert apidiff._resolve_old_location("pkg.foo", None, tmp_path / "old") is None


def test_new_function_added_is_not_breaking(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo():\n    return 1\n"}
    new = {"pkg/__init__.py": "def foo():\n    return 1\n\ndef bar():\n    return 2\n"}

    result = diff_of(tmp_path, old, new)

    assert result.status == "ok"
    assert result.changes == []


def test_removed_submodule(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "", "pkg/sub.py": "def f():\n    return 1\n"}
    new = {"pkg/__init__.py": ""}

    result = diff_of(tmp_path, old, new)

    assert [(c.kind, c.symbol, c.severity) for c in result.changes] == [
        ("OBJECT_REMOVED", "pkg.sub", "high")
    ]


def test_top_level_name_differs_from_distribution(tmp_path: Path) -> None:
    sources = {"mypkg/__init__.py": "def foo():\n    return 1\n"}

    result = diff_of(tmp_path, sources, sources, name="my-pkg", top_level=("mypkg",))

    assert result.status == "ok"
    assert result.top_level == ["mypkg"]
    assert result.compared == ["mypkg"]
    assert result.package == "my-pkg"


def test_top_level_from_record(tmp_path: Path) -> None:
    sources = {"mypkg/__init__.py": "def foo():\n    return 1\n"}

    result = diff_of(tmp_path, sources, sources, name="my-pkg", top_level=None, record=True)

    assert result.status == "ok"
    assert result.top_level == ["mypkg"]


def test_compiled_only_wheel_has_no_python_sources(tmp_path: Path) -> None:
    sources = {"pkg/native.pyd": b"\x00\x01\x02"}

    result = diff_of(tmp_path, sources, sources)

    assert result.status == "no_api_diff"
    assert result.reason == "no_python_sources"


def test_no_wheel(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "info": {"name": "pkg", "version": "1.0.0", "yanked": False},
            "urls": [
                {
                    "filename": "pkg-1.0.0.tar.gz",
                    "url": f"{FILES}/pkg-1.0.0.tar.gz",
                    "packagetype": "sdist",
                    "digests": {},
                }
            ],
        }
    ).encode("utf-8")
    fetcher = FakeFetcher({f"{PYPI}/pkg/1.0.0/json": raw})

    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=tmp_path / "cache", fetcher=fetcher)

    assert result.status == "no_api_diff"
    assert result.reason == "no_wheel"


def test_hash_mismatch_leaves_cache_empty(tmp_path: Path) -> None:
    wheel = make_wheel("1.0.0", {"pkg/__init__.py": "def foo():\n    return 1\n"})
    filename = wheel_filename("pkg", "1.0.0")
    raw = json.dumps(
        {
            "info": {"name": "pkg", "version": "1.0.0", "yanked": False},
            "urls": [
                {
                    "filename": filename,
                    "url": f"{FILES}/{filename}",
                    "packagetype": "bdist_wheel",
                    "digests": {"sha256": "0" * 64},
                }
            ],
        }
    ).encode("utf-8")
    fetcher = FakeFetcher({f"{PYPI}/pkg/1.0.0/json": raw, f"{FILES}/{filename}": wheel})

    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=tmp_path / "cache", fetcher=fetcher)

    assert result.status == "no_api_diff"
    assert result.reason == "hash_mismatch"
    assert not (tmp_path / "cache" / "pkg" / "1.0.0").exists()


def test_zip_slip_is_rejected_and_leaves_cache_empty(tmp_path: Path) -> None:
    sources = {"pkg/__init__.py": "", "../evil.py": "x = 1\n"}
    wheel = make_wheel("1.0.0", sources)
    fetcher = FakeFetcher(responses_for("pkg", "1.0.0", wheel))

    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=tmp_path / "cache", fetcher=fetcher)

    assert result.status == "no_api_diff"
    assert result.reason == "unsafe_archive"
    assert not (tmp_path / "cache" / "pkg" / "1.0.0").exists()
    assert not (tmp_path / "cache" / "evil.py").exists()


def test_size_cap_uses_written_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(apidiff, "_MAX_TOTAL_BYTES", 1024)
    original_init = zipfile.ZipExtFile.__init__

    def patched_init(self, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        self._left = 10**12

    monkeypatch.setattr(zipfile.ZipExtFile, "__init__", patched_init)

    big = "value = 1\n" * 5000
    wheel = make_wheel("1.0.0", {"pkg/__init__.py": big})
    wheel = shrink_central_uncompressed_size(wheel, "pkg/__init__.py", 10)
    fetcher = FakeFetcher(responses_for("pkg", "1.0.0", wheel))

    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=tmp_path / "cache", fetcher=fetcher)

    assert result.status == "no_api_diff"
    assert result.reason == "too_large"
    assert not (tmp_path / "cache" / "pkg" / "1.0.0").exists()


def test_unpack_failure_leaves_cache_empty(tmp_path: Path, monkeypatch) -> None:
    wheel = make_wheel("1.0.0", {"pkg/__init__.py": "def foo():\n    return 1\n"})
    fetcher = FakeFetcher(responses_for("pkg", "1.0.0", wheel))

    def boom(*args: object, **kwargs: object) -> int:
        raise OSError("path too long")

    monkeypatch.setattr(apidiff, "_extract_member", boom)

    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=tmp_path / "cache", fetcher=fetcher)

    assert result.status == "no_api_diff"
    assert result.reason == "unpack_failed"
    assert any("OSError" in warning for warning in result.warnings)
    assert not (tmp_path / "cache" / "pkg" / "1.0.0").exists()


def test_unparsable_sources(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo():\n    return 1\n"}
    new = {"pkg/__init__.py": "def (:\n"}

    result = diff_of(tmp_path, old, new)

    assert result.status == "no_api_diff"
    assert result.reason == "unparsable_sources"
    assert any("pkg/__init__.py" in warning for warning in result.warnings)


def test_latin1_source_is_not_unparsable(tmp_path: Path) -> None:
    legacy = (
        b"# -*- coding: latin-1 -*-\n"
        b"\n"
        b'GREETING = "caf\xe9"\n'
        b"\n"
        b"\n"
        b"def legacy():\n"
        b"    return GREETING\n"
    )
    sources = {
        "pkg/__init__.py": "def foo():\n    return 1\n",
        "pkg/scripts/legacy.py": legacy,
    }

    result = diff_of(tmp_path, sources, sources)

    assert result.status == "ok"
    assert result.reason is None
    assert result.changes == []


def test_null_byte_is_unparsable(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo():\n    return 1\n"}
    new = {"pkg/__init__.py": b"x = 1\x00\n"}

    result = diff_of(tmp_path, old, new)

    assert result.status == "no_api_diff"
    assert result.reason == "unparsable_sources"
    assert any("pkg/__init__.py" in warning for warning in result.warnings)


def test_yanked_version_warns(tmp_path: Path) -> None:
    sources = {"pkg/__init__.py": "def foo():\n    return 1\n"}
    old_wheel = make_wheel("1.0.0", sources)
    new_wheel = make_wheel("2.0.0", sources)
    fetcher = FakeFetcher(
        {
            **responses_for("pkg", "1.0.0", old_wheel, yanked=True),
            **responses_for("pkg", "2.0.0", new_wheel),
        }
    )

    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=tmp_path / "cache", fetcher=fetcher)

    assert result.status == "ok"
    assert any("yanked" in warning for warning in result.warnings)


def test_diff_reports_differences_between_versions(tmp_path: Path) -> None:
    old = {"pkg/__init__.py": "def foo(a, b):\n    return a + b\n"}
    new = {"pkg/__init__.py": "def foo(a):\n    return a\n"}

    result = diff_of(tmp_path, old, new)

    assert result.status == "ok"
    assert [c.symbol for c in result.changes] == ["pkg.foo"]
    assert result.changes[0].new_location is not None
    assert result.changes[0].new_location.file == "pkg/__init__.py"


def test_cache_is_reused(tmp_path: Path) -> None:
    sources = {"pkg/__init__.py": "def foo():\n    return 1\n"}
    old_wheel = make_wheel("1.0.0", sources)
    new_wheel = make_wheel("2.0.0", sources)
    fetcher = FakeFetcher(
        {**responses_for("pkg", "1.0.0", old_wheel), **responses_for("pkg", "2.0.0", new_wheel)}
    )
    cache = tmp_path / "cache"

    first = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=cache, fetcher=fetcher)
    calls_after_first = len(fetcher.calls)
    second = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=cache, fetcher=fetcher)

    assert len(fetcher.calls) == calls_after_first
    assert second == first


def test_severity_table_covers_every_breakage_kind() -> None:
    assert {kind.name for kind in griffe.BreakageKind} == set(apidiff._SEVERITY)


def test_wheels_are_chosen_deterministically(tmp_path: Path) -> None:
    sources = {"pkg/__init__.py": "def foo():\n    return 1\n"}
    wheel = make_wheel("1.0.0", sources)
    plain = "pkg-1.0.0-py3-none-any.whl"
    platform = "pkg-1.0.0-cp311-cp311-win_amd64.whl"
    raw = json.dumps(
        {
            "info": {"name": "pkg", "version": "1.0.0", "yanked": False},
            "urls": [
                {
                    "filename": platform,
                    "url": f"{FILES}/{platform}",
                    "packagetype": "bdist_wheel",
                    "digests": {"sha256": hashlib.sha256(wheel).hexdigest()},
                },
                {
                    "filename": plain,
                    "url": f"{FILES}/{plain}",
                    "packagetype": "bdist_wheel",
                    "digests": {"sha256": hashlib.sha256(wheel).hexdigest()},
                },
            ],
        }
    ).encode("utf-8")
    fetcher = FakeFetcher({f"{PYPI}/pkg/1.0.0/json": raw, f"{FILES}/{plain}": wheel})

    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=tmp_path / "cache", fetcher=fetcher)

    assert result.reason == "no_wheel" or result.old_wheel == plain


def test_cli_diff_prints_json(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    source = "def foo(a, b=1):\n    return a + b\n"
    for version in ("1.0.0", "2.0.0"):
        target = cache / "pkg" / version
        (target / "pkg").mkdir(parents=True)
        (target / "pkg" / "__init__.py").write_text(source, encoding="utf-8")
        dist_info = target / f"pkg-{version}.dist-info"
        dist_info.mkdir()
        (dist_info / "top_level.txt").write_text("pkg\n", encoding="utf-8")
        (target / ".bumpcheck-wheel").write_text(wheel_filename("pkg", version), encoding="utf-8")

    result = CliRunner().invoke(app, ["diff", "pkg", "1.0.0", "2.0.0", "--cache-dir", str(cache)])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["package"] == "pkg"
    assert payload["status"] == "ok"
    assert payload["old_wheel"] == "pkg-1.0.0-py3-none-any.whl"


def test_cli_diff_rejects_invalid_version(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["diff", "pkg", "nope", "2.0.0", "--cache-dir", str(tmp_path / "cache")]
    )

    assert result.exit_code == 2


def test_cli_diff_rejects_empty_name(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["diff", "  ", "1.0.0", "2.0.0", "--cache-dir", str(tmp_path / "cache")]
    )

    assert result.exit_code == 2


def test_diff_versions_exposes_requires_python_metadata(tmp_path: Path) -> None:
    old_wheel = make_wheel("1.0.0", {"pkg/__init__.py": "x = 1\n"})
    new_wheel = make_wheel("2.0.0", {"pkg/__init__.py": "x = 2\n"})
    fetcher = FakeFetcher(
        {
            **responses_for("pkg", "1.0.0", old_wheel, requires_python=">=3.8"),
            **responses_for("pkg", "2.0.0", new_wheel, requires_python=">=3.10"),
        }
    )

    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=tmp_path / "cache", fetcher=fetcher)

    assert result.status == "ok"
    assert result.old_requires_python == ">=3.8"
    assert result.new_requires_python == ">=3.10"
    assert result.old_requires_python_state == "available"
    assert result.new_requires_python_state == "available"


def test_diff_versions_reuses_cached_requires_python_metadata(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    old_wheel = make_wheel("1.0.0", {"pkg/__init__.py": "x = 1\n"})
    new_wheel = make_wheel("2.0.0", {"pkg/__init__.py": "x = 2\n"})
    first = FakeFetcher(
        {
            **responses_for("pkg", "1.0.0", old_wheel, requires_python=">=3.8"),
            **responses_for("pkg", "2.0.0", new_wheel, requires_python=">=3.10"),
        }
    )
    diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=cache, fetcher=first)

    second = FakeFetcher({})
    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=cache, fetcher=second)

    assert result.status == "ok"
    assert result.old_requires_python == ">=3.8"
    assert result.new_requires_python == ">=3.10"
    assert second.calls == []


def test_diff_versions_missing_metadata_yields_none(tmp_path: Path) -> None:
    old_wheel = make_wheel("1.0.0", {"pkg/__init__.py": "x = 1\n"})
    new_wheel = make_wheel("2.0.0", {"pkg/__init__.py": "x = 2\n"})
    fetcher = FakeFetcher(
        {
            **responses_for("pkg", "1.0.0", old_wheel),
            **responses_for("pkg", "2.0.0", new_wheel),
        }
    )

    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=tmp_path / "cache", fetcher=fetcher)

    assert result.status == "ok"
    assert result.old_requires_python is None
    assert result.new_requires_python is None
    assert result.old_requires_python_state == "available"
    assert result.new_requires_python_state == "available"


def test_apidiff_cache_hit_without_sidecar_refetches_metadata(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    old_wheel = make_wheel("1.0.0", {"pkg/__init__.py": "x = 1\n"})
    new_wheel = make_wheel("2.0.0", {"pkg/__init__.py": "x = 2\n"})
    first = FakeFetcher(
        {
            **responses_for("pkg", "1.0.0", old_wheel),
            **responses_for("pkg", "2.0.0", new_wheel),
        }
    )
    diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=cache, fetcher=first)
    for version in ("1.0.0", "2.0.0"):
        (cache / "_metadata" / f"pkg__{version}.json").unlink(missing_ok=True)

    refetch = FakeFetcher(
        {
            f"{PYPI}/pkg/1.0.0/json": metadata(
                "pkg", "1.0.0", wheel_filename("pkg", "1.0.0"), old_wheel, requires_python=">=3.8"
            ),
            f"{PYPI}/pkg/2.0.0/json": metadata(
                "pkg", "2.0.0", wheel_filename("pkg", "2.0.0"), new_wheel, requires_python=">=3.10"
            ),
        }
    )
    result = diff_versions("pkg", "1.0.0", "2.0.0", cache_dir=cache, fetcher=refetch)

    assert result.status == "ok"
    assert result.old_requires_python == ">=3.8"
    assert result.new_requires_python == ">=3.10"
    assert sorted(refetch.calls) == [
        f"{PYPI}/pkg/1.0.0/json",
        f"{PYPI}/pkg/2.0.0/json",
    ]
    assert (cache / "_metadata" / "pkg__1.0.0.json").is_file()
