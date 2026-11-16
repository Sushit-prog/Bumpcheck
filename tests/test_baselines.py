from __future__ import annotations

import pytest

from bumpcheck.baselines import always_safe, semver_rule
from bumpcheck.models import Bump


def make_bump(old: str, new: str) -> Bump:
    return Bump(package="pkg", old_version=old, new_version=new, source="uv.lock")


def test_always_safe() -> None:
    assert always_safe(make_bump("1.0.0", "2.0.0")) == "SAFE"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("1.2.3", "2.0.0", "REVIEW"),
        ("0.1.0", "0.2.0", "REVIEW"),
        ("1.2.3", "1.2.4", "SAFE"),
        ("1.2.3", "1.3.0", "SAFE"),
        ("0.1.0", "0.1.1", "SAFE"),
        ("1.0.0", "2.0.0a1", "REVIEW"),
        ("1.0.0a1", "1.0.0", "SAFE"),
    ],
)
def test_semver_rule(old: str, new: str, expected: str) -> None:
    assert semver_rule(make_bump(old, new)) == expected
