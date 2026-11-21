from __future__ import annotations

import pytest
from packaging.version import Version

from bumpcheck.pycompat import (
    ConcretePythonVersion,
    NonConcretePythonVersion,
    allows_python,
    assess_requires_python_change,
    interpret_python_version,
    newly_disallowed,
    normalize_python_version,
    python_floor,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3.9", Version("3.9")),
        ("3.9.1", Version("3.9")),
        ("3.10", Version("3.10")),
        ("3.10.4", Version("3.10")),
        ("3.13t", Version("3.13")),
        ("3.14-dev", Version("3.14")),
        ("3.12.0-rc.1", Version("3.12")),
        ("3.11.0-beta.2", Version("3.11")),
    ],
)
def test_normalize_python_version_accepts_concrete_forms(text: str, expected: Version) -> None:
    assert normalize_python_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not-a-version",
        "x.y",
        "3.x",
        "3",
        "3.x.y",
        "pypy-3.9",
        "stable",
        ">=3.9",
        "${{ matrix.python }}",
    ],
)
def test_normalize_python_version_rejects_non_concrete(text: str) -> None:
    assert normalize_python_version(text) is None


@pytest.mark.parametrize(
    ("text", "major", "minor"),
    [
        ("3.10", 3, 10),
        ("3.10.4", 3, 10),
        ("3.13t", 3, 13),
        ("3.14-dev", 3, 14),
        ("3.12.0-rc.1", 3, 12),
        ("3.11.0-beta.2", 3, 11),
    ],
)
def test_interpret_python_version_concrete(text: str, major: int, minor: int) -> None:
    assert interpret_python_version(text) == ConcretePythonVersion(major=major, minor=minor)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("3.x", "wildcard"),
        ("3", "wildcard"),
        ("3.x.y", "wildcard"),
        ("${{ matrix.python }}", "expression"),
        ("${{ inputs.v }}", "expression"),
        ("stable", "alias"),
        ("latest", "alias"),
        (">=3.9", "range"),
        ("~3.10", "range"),
        ("3.9 - 3.11", "range"),
        ("pypy-3.9", "implementation"),
        ("cpython-3.10", "implementation"),
        ("python-version-file", "file"),
        ("not-a-version", "unparsable"),
    ],
)
def test_interpret_python_version_non_concrete(text: str, reason: str) -> None:
    assert interpret_python_version(text) == NonConcretePythonVersion(raw=text, reason=reason)


def test_interpret_python_version_file_source() -> None:
    assert interpret_python_version(".python-version", source="file") == NonConcretePythonVersion(
        raw=".python-version", reason="file"
    )


def test_float_input_is_not_guessed_as_a_version() -> None:
    assert interpret_python_version(3.10) == NonConcretePythonVersion(raw="", reason="unparsable")
    assert allows_python(">=3.10", 3.10) is None


def test_allows_python_missing_spec_returns_none() -> None:
    assert allows_python(None, "3.10") is None
    assert allows_python("", "3.10") is None
    assert allows_python("   ", "3.10") is None


def test_allows_python_malformed_spec_returns_none_without_raising() -> None:
    assert allows_python("not a spec", "3.10") is None
    assert allows_python(">>3.10", "3.10") is None
    assert allows_python(">=3.10", "junk") is None


def test_allows_python_returns_none_for_non_concrete_input() -> None:
    assert allows_python(">=3.10", "3.x") is None
    assert allows_python(">=3.10", "3") is None
    assert allows_python(">=3.10", "${{ matrix.python }}") is None
    assert allows_python(">=3.10", "pypy-3.10") is None
    assert allows_python(">=3.10", ">=3.9") is None
    assert allows_python(">=3.10", NonConcretePythonVersion(raw="3.x", reason="wildcard")) is None


def test_allows_python_evaluates_spec_against_versions() -> None:
    assert allows_python(">=3.10", "3.9") is False
    assert allows_python(">=3.10", "3.10") is True
    assert allows_python(">=3.10", Version("3.11")) is True
    assert allows_python(">=3.8,<3.11", "3.11") is False
    assert allows_python(">=3.0,<4", ConcretePythonVersion(major=3, minor=10)) is True
    assert allows_python(">=3.0,<4", "3.10.4") is True


def test_python_floor_from_spec() -> None:
    assert python_floor(">=3.10") == Version("3.10")
    assert python_floor(">=3.8,<3.13") == Version("3.8")
    assert python_floor("==3.9.*") == Version("3.9")
    assert python_floor(">=3.9") == Version("3.9")


def test_python_floor_requires_lower_bound_and_python_three() -> None:
    assert python_floor("<3.12") is None
    assert python_floor(">=2.7,<3") is None


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


def test_newly_disallowed_skips_non_concrete_versions() -> None:
    versions = ["junk", "3.x", ">=3.9", "pypy-3.9", "${{ inputs.v }}", "", "3.9"]
    assert newly_disallowed(">=3.8", ">=3.10", versions) == ["3.9"]


def test_assess_requires_python_change_detects_tightening() -> None:
    result = assess_requires_python_change(">=3.8", ">=3.10", ["3.8", "3.9", "3.10"])
    assert result.decided is True
    assert result.tightened is True
    assert result.excluded == ("3.8", "3.9")


def test_assess_requires_python_change_loosened_does_not_fire() -> None:
    result = assess_requires_python_change(">=3.10", ">=3.8", ["3.8", "3.9", "3.10"])
    assert result.decided is True
    assert result.tightened is False
    assert result.excluded == ()


def test_assess_requires_python_change_unchanged_does_not_fire() -> None:
    result = assess_requires_python_change(">=3.9", ">=3.9", ["3.8", "3.9", "3.10"])
    assert result.decided is True
    assert result.tightened is False


def test_assess_requires_python_change_null_semantics() -> None:
    both_null = assess_requires_python_change(None, None, ["3.8", "3.9"])
    assert both_null.decided is True
    assert both_null.tightened is False

    old_null = assess_requires_python_change(None, ">=3.10", ["3.8", "3.9", "3.10"])
    assert old_null.tightened is True
    assert old_null.excluded == ("3.8", "3.9")

    new_null = assess_requires_python_change(">=3.10", None, ["3.8", "3.9", "3.10"])
    assert new_null.tightened is False


def test_assess_requires_python_change_outside_universe_is_evaluated() -> None:
    result = assess_requires_python_change(">=3.6", ">=3.10", ["3.6", "3.7"])
    assert result.excluded == ("3.6", "3.7", "3.8", "3.9")


def test_assess_requires_python_change_malformed_spec_is_unknown() -> None:
    result = assess_requires_python_change("oops", ">=3.10", ["3.8"])
    assert result.decided is False
    assert any("invalid old" in warning for warning in result.warnings)


def test_assess_requires_python_change_unavailable_metadata_is_unknown() -> None:
    result = assess_requires_python_change(">=3.8", ">=3.10", ["3.8"], state="unavailable")
    assert result.decided is False
    assert result.state == "unavailable"
