from __future__ import annotations

from pathlib import Path

import pytest

from bumpcheck.lockdiff import LockParseError, parse_lock_pair

FIXTURES = Path(__file__).parent / "fixtures"

KIND_BY_DIR = {
    "uv_lock": "uv.lock",
    "poetry_lock": "poetry.lock",
    "requirements": "requirements",
}

CASES = [
    ("uv_lock", "single_bump", [("packaging", "24.0", "25.0")]),
    (
        "uv_lock",
        "multiple_bumps",
        [("packaging", "24.0", "25.0"), ("requests", "2.31.0", "2.32.0")],
    ),
    ("uv_lock", "downgrade", [("flask", "3.1.0", "3.0.3")]),
    (
        "uv_lock",
        "added_removed",
        [("httpx", None, "0.27.2"), ("requests", "2.31.0", None)],
    ),
    ("uv_lock", "no_changes", []),
    ("uv_lock", "equivalent_versions", []),
    ("poetry_lock", "single_bump", [("packaging", "24.0", "25.0")]),
    (
        "poetry_lock",
        "multiple_bumps",
        [("packaging", "24.0", "25.0"), ("requests", "2.31.0", "2.32.0")],
    ),
    ("poetry_lock", "downgrade", [("flask", "3.1.0", "3.0.3")]),
    (
        "poetry_lock",
        "added_removed",
        [("httpx", None, "0.27.2"), ("requests", "2.31.0", None)],
    ),
    ("poetry_lock", "no_changes", []),
    ("requirements", "single_bump", [("certifi", "2024.2.2", "2024.8.30")]),
    (
        "requirements",
        "multiple_bumps",
        [("packaging", "24.0", "25.0"), ("requests", "2.31.0", "2.32.0")],
    ),
    ("requirements", "downgrade", [("flask", "3.1.0", "3.0.3")]),
    (
        "requirements",
        "added_removed",
        [("httpx", None, "0.27.2"), ("requests", "2.31.0", None)],
    ),
    ("requirements", "extras_markers", [("requests", "2.31.0", "2.32.0")]),
    ("requirements", "no_changes", []),
    ("requirements", "pip_compile", [("certifi", "2024.2.2", "2024.8.30")]),
]


def fixture_pair(kind_dir: str, scenario: str) -> tuple[str, str]:
    base = FIXTURES / kind_dir / scenario
    old = (base / "old.lock").read_text(encoding="utf-8")
    new = (base / "new.lock").read_text(encoding="utf-8")
    return old, new


def bump_tuples(result: object) -> list[tuple[str, str | None, str | None]]:
    return [(bump.package, bump.old_version, bump.new_version) for bump in result.bumps]


def unresolved_tuples(result: object) -> list[tuple[str, list[str], list[str]]]:
    return [(item.package, item.removed, item.added) for item in result.unresolved]


@pytest.mark.parametrize(("kind_dir", "scenario", "expected"), CASES)
def test_fixture_bumps(kind_dir: str, scenario: str, expected: list) -> None:
    old, new = fixture_pair(kind_dir, scenario)
    result = parse_lock_pair(old, new, KIND_BY_DIR[kind_dir])
    assert bump_tuples(result) == expected
    assert result.warnings == []


def test_bumps_are_sorted_by_package_name() -> None:
    old, new = fixture_pair("requirements", "multiple_bumps")
    result = parse_lock_pair(old, new, "requirements")
    assert [bump.package for bump in result.bumps] == ["packaging", "requests"]


def test_no_version_package_is_ignored_with_warning() -> None:
    old, new = fixture_pair("uv_lock", "no_version")
    result = parse_lock_pair(old, new, "uv.lock")
    assert bump_tuples(result) == [("packaging", "24.0", "25.0")]
    assert any("mystery" in warning for warning in result.warnings)


@pytest.mark.parametrize("kind_dir", ["uv_lock", "poetry_lock", "requirements"])
def test_forked_unchanged_has_no_bumps_unresolved_or_warnings(kind_dir: str) -> None:
    old, new = fixture_pair(kind_dir, "forked_unchanged")
    result = parse_lock_pair(old, new, KIND_BY_DIR[kind_dir])
    assert result.bumps == []
    assert result.unresolved == []
    assert result.warnings == []


@pytest.mark.parametrize("kind_dir", ["uv_lock", "poetry_lock", "requirements"])
def test_forked_one_side_changes_is_unresolved_not_a_bump(kind_dir: str) -> None:
    old, new = fixture_pair(kind_dir, "forked_one_side_changes")
    result = parse_lock_pair(old, new, KIND_BY_DIR[kind_dir])
    assert result.bumps == []
    assert unresolved_tuples(result) == [("foo", ["1.0"], ["1.1"])]
    assert any("foo" in warning for warning in result.warnings)


@pytest.mark.parametrize("kind_dir", ["uv_lock", "requirements"])
def test_single_to_forked_is_unresolved(kind_dir: str) -> None:
    old, new = fixture_pair(kind_dir, "single_to_forked")
    result = parse_lock_pair(old, new, KIND_BY_DIR[kind_dir])
    assert result.bumps == []
    assert unresolved_tuples(result) == [("foo", ["1.0"], ["1.1", "2.0"])]
    assert any("foo" in warning for warning in result.warnings)


@pytest.mark.parametrize("kind_dir", ["uv_lock", "requirements"])
def test_forked_package_added_on_one_side_is_unresolved(kind_dir: str) -> None:
    old, new = fixture_pair(kind_dir, "forked_added")
    result = parse_lock_pair(old, new, KIND_BY_DIR[kind_dir])
    assert result.bumps == []
    assert unresolved_tuples(result) == [("foo", [], ["1.0", "2.0"])]


@pytest.mark.parametrize("kind_dir", ["uv_lock", "requirements"])
def test_forked_package_removed_on_one_side_is_unresolved(kind_dir: str) -> None:
    old, new = fixture_pair(kind_dir, "forked_removed")
    result = parse_lock_pair(old, new, KIND_BY_DIR[kind_dir])
    assert result.bumps == []
    assert unresolved_tuples(result) == [("foo", ["1.0", "2.0"], [])]


def test_requirements_marker_split_unchanged_is_ignored() -> None:
    old, new = fixture_pair("requirements", "marker_split")
    result = parse_lock_pair(old, new, "requirements")
    assert result.bumps == []
    assert result.unresolved == []
    assert result.warnings == []


def test_single_version_add_and_remove_stay_normal_bumps() -> None:
    old, new = fixture_pair("uv_lock", "added_removed")
    result = parse_lock_pair(old, new, "uv.lock")
    assert bump_tuples(result) == [("httpx", None, "0.27.2"), ("requests", "2.31.0", None)]
    assert result.unresolved == []


def test_unresolved_is_sorted_by_package() -> None:
    def uv(*entries: tuple[str, str]) -> str:
        blocks = "".join(
            f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            for name, version in entries
        )
        return f'version = 1\nrevision = 1\nrequires-python = ">=3.11"\n{blocks}'

    old = uv(("zeta", "1.0"), ("zeta", "2.0"), ("alpha", "1.0"), ("alpha", "2.0"))
    new = uv(("zeta", "1.1"), ("zeta", "2.0"), ("alpha", "1.1"), ("alpha", "2.0"))
    result = parse_lock_pair(old, new, "uv.lock")
    assert [item.package for item in result.unresolved] == ["alpha", "zeta"]


def test_root_project_change_is_skipped() -> None:
    old, new = fixture_pair("uv_lock", "root_project")
    result = parse_lock_pair(old, new, "uv.lock")
    assert result.bumps == []
    assert any("bumpcheck" in warning for warning in result.warnings)
    assert any("an 'editable'" in warning for warning in result.warnings)


def test_equivalent_versions_are_not_a_bump() -> None:
    old, new = fixture_pair("uv_lock", "equivalent_versions")
    result = parse_lock_pair(old, new, "uv.lock")
    assert result.bumps == []


def test_unpinned_and_unsupported_requirements_warn() -> None:
    old, new = fixture_pair("requirements", "unpinned")
    result = parse_lock_pair(old, new, "requirements")
    assert bump_tuples(result) == [("click", "8.1.7", "8.1.8")]
    warnings = " ".join(result.warnings)
    assert "flask" in warnings
    assert "wildcard" in warnings
    assert "example.com" in warnings
    assert "other.txt" not in warnings
    assert "index-url" not in warnings


def test_pip_compile_hashes_and_continuations_are_ignored() -> None:
    old, new = fixture_pair("requirements", "pip_compile")
    result = parse_lock_pair(old, new, "requirements")
    assert bump_tuples(result) == [("certifi", "2024.2.2", "2024.8.30")]
    assert result.warnings == []


def test_versionless_entry_on_one_side_is_ignored() -> None:
    old = (
        "version = 1\n"
        "\n"
        "[[package]]\n"
        'name = "mystery"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    new = (
        "version = 1\n"
        "\n"
        "[[package]]\n"
        'name = "mystery"\n'
        'version = "2.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    result = parse_lock_pair(old, new, "uv.lock")
    assert result.bumps == []
    assert any("mystery" in warning for warning in result.warnings)


def test_malformed_toml_raises_lock_parse_error() -> None:
    _, valid = fixture_pair("uv_lock", "single_bump")
    with pytest.raises(LockParseError):
        parse_lock_pair('[[package]\nname = "x"\n', valid, "uv.lock")


def test_empty_toml_raises_lock_parse_error() -> None:
    _, valid = fixture_pair("poetry_lock", "single_bump")
    with pytest.raises(LockParseError):
        parse_lock_pair("", valid, "poetry.lock")
    with pytest.raises(LockParseError):
        parse_lock_pair(valid, "   \n", "poetry.lock")


def test_unknown_kind_raises_lock_parse_error() -> None:
    old, new = fixture_pair("uv_lock", "single_bump")
    with pytest.raises(LockParseError):
        parse_lock_pair(old, new, "cargo.lock")


def test_requirements_crlf_matches_lf() -> None:
    old, new = fixture_pair("requirements", "single_bump")
    result = parse_lock_pair(
        old.replace("\n", "\r\n"),
        new.replace("\n", "\r\n"),
        "requirements",
    )
    assert bump_tuples(result) == [("certifi", "2024.2.2", "2024.8.30")]


def test_toml_crlf_matches_lf() -> None:
    old, new = fixture_pair("uv_lock", "single_bump")
    result = parse_lock_pair(
        old.replace("\n", "\r\n"),
        new.replace("\n", "\r\n"),
        "uv.lock",
    )
    assert bump_tuples(result) == [("packaging", "24.0", "25.0")]


def test_utf8_bom_is_stripped() -> None:
    old, new = fixture_pair("uv_lock", "single_bump")
    result = parse_lock_pair(f"\ufeff{old}", f"\ufeff{new}", "uv.lock")
    assert bump_tuples(result) == [("packaging", "24.0", "25.0")]


def iter_fixture_pairs() -> list:
    params = []
    for kind_dir in KIND_BY_DIR:
        base = FIXTURES / kind_dir
        for scenario_dir in sorted(path for path in base.iterdir() if path.is_dir()):
            params.append(
                pytest.param(kind_dir, scenario_dir.name, id=f"{kind_dir}-{scenario_dir.name}")
            )
    return params


@pytest.mark.parametrize(("kind_dir", "scenario"), iter_fixture_pairs())
def test_every_fixture_parses_or_raises_lock_parse_error(kind_dir: str, scenario: str) -> None:
    old, new = fixture_pair(kind_dir, scenario)
    try:
        parse_lock_pair(old, new, KIND_BY_DIR[kind_dir])
    except LockParseError:
        pass
