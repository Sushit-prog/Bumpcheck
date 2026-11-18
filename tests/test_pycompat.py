from __future__ import annotations

from packaging.version import Version

from bumpcheck.pycompat import (
    allows_python,
    newly_disallowed,
    normalize_python_version,
    python_floor,
)


def test_normalize_python_version_accepts_common_forms() -> None:
    assert normalize_python_version("3.9") == Version("3.9")
    assert normalize_python_version("3.9.1") == Version("3.9.1")
    assert normalize_python_version("3.10") == Version("3.10")
    assert normalize_python_version("3.x") == Version("3.0")
    assert normalize_python_version("3") == Version("3.0")


def test_normalize_python_version_rejects_junk() -> None:
    assert normalize_python_version("") is None
    assert normalize_python_version("   ") is None
    assert normalize_python_version("not-a-version") is None
    assert normalize_python_version("x.y") is None


def test_allows_python_missing_spec_returns_none() -> None:
    assert allows_python(None, "3.10") is None
    assert allows_python("", "3.10") is None
    assert allows_python("   ", "3.10") is None


def test_allows_python_malformed_spec_returns_none_without_raising() -> None:
    assert allows_python("not a spec", "3.10") is None
    assert allows_python(">>3.10", "3.10") is None
    assert allows_python(">=3.10", "junk") is None


def test_allows_python_evaluates_spec_against_versions() -> None:
    assert allows_python(">=3.10", "3.9") is False
    assert allows_python(">=3.10", "3.10") is True
    assert allows_python(">=3.10", Version("3.11")) is True
    assert allows_python(">=3.8,<3.11", "3.11") is False
    assert allows_python(">=3.0,<4", "3.x") is True


def test_python_floor_from_spec() -> None:
    assert python_floor(">=3.10") == Version("3.10")
    assert python_floor(">=3.8,<3.13") == Version("3.8")
    assert python_floor("==3.9.*") == Version("3.9")


def test_python_floor_missing_or_malformed_returns_none() -> None:
    assert python_floor(None) is None
    assert python_floor("") is None
    assert python_floor("broken") is None


def test_newly_disallowed_empty_for_equal_specs() -> None:
    assert newly_disallowed(">=3.8", ">=3.8", ["3.8", "3.9", "3.10"]) == []


def test_newly_disallowed_for_tightened_spec() -> None:
    versions = ["3.8", "3.9", "3.10", "3.11"]
    assert newly_disallowed(">=3.8", ">=3.10", versions) == ["3.8", "3.9"]


def test_newly_disallowed_empty_for_missing_or_malformed_specs() -> None:
    versions = ["3.8", "3.9"]
    assert newly_disallowed(None, ">=3.10", versions) == []
    assert newly_disallowed(">=3.8", None, versions) == []
    assert newly_disallowed("oops", ">=3.10", versions) == []
    assert newly_disallowed(">=3.8", "oops", versions) == []


def test_newly_disallowed_skips_unparseable_versions() -> None:
    assert newly_disallowed(">=3.8", ">=3.10", ["junk", "3.9", ""]) == ["3.9"]
