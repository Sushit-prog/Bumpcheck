from __future__ import annotations

import pytest
from pydantic import ValidationError

from bumpcheck.models import Bump, ParseResult


def test_package_name_is_canonicalized() -> None:
    bump = Bump(
        package="My_Package",
        old_version="1.0",
        new_version="2.0",
        source="uv.lock",
    )
    assert bump.package == "my-package"


def test_add_bump_allows_missing_old_version() -> None:
    bump = Bump(package="requests", old_version=None, new_version="2.31.0", source="requirements")
    assert bump.old_version is None


def test_removal_bump_allows_missing_new_version() -> None:
    bump = Bump(package="requests", old_version="2.31.0", new_version=None, source="poetry.lock")
    assert bump.new_version is None


def test_both_versions_none_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Bump(package="requests", old_version=None, new_version=None, source="uv.lock")


def test_equal_versions_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Bump(package="requests", old_version="2.31.0", new_version="2.31.0", source="uv.lock")


def test_invalid_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Bump(package="requests", old_version="not-a-version", new_version="2.0", source="uv.lock")


def test_invalid_source_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Bump(package="requests", old_version="1.0", new_version="2.0", source="cargo.lock")


def test_bump_is_frozen() -> None:
    bump = Bump(package="requests", old_version="1.0", new_version="2.0", source="uv.lock")
    with pytest.raises(ValidationError):
        bump.new_version = "3.0"


def test_parse_result_holds_bumps_and_warnings() -> None:
    result = ParseResult(bumps=[], warnings=["heads up"])
    assert result.bumps == []
    assert result.warnings == ["heads up"]
