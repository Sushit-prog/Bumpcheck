from __future__ import annotations

import pytest

from bumpcheck.bench.titles import (
    classify_title,
    loose_version_match,
    parse_dependabot_title,
    parse_renovate_title,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bump requests from 2.31.0 to 2.32.0", ("requests", "2.31.0", "2.32.0", None)),
        ("bump Django from 4.2 to 5.0", ("Django", "4.2", "5.0", None)),
        (
            "chore(deps): bump requests from 2.31.0 to 2.32.0",
            ("requests", "2.31.0", "2.32.0", None),
        ),
        (
            "Bump requests from 2.31.0 to 2.32.0 in /backend",
            ("requests", "2.31.0", "2.32.0", "/backend"),
        ),
        (
            "Bump requests from 2.31.0 to 2.32.0 in the pip group across 1 directory",
            ("requests", "2.31.0", "2.32.0", None),
        ),
    ],
)
def test_parse_dependabot_title(title: str, expected: tuple) -> None:
    parsed = parse_dependabot_title(title)
    assert parsed is not None
    assert (parsed.name, parsed.old_version, parsed.new_version, parsed.path) == expected
    assert parsed.bot == "dependabot"


@pytest.mark.parametrize(
    "title",
    [
        "Bump the pip group across 1 directory with 3 updates",
        "Bump requests from 2.31.0 to 2.32.0 and 2.33.0",
        "Update requests",
    ],
)
def test_parse_dependabot_title_rejects(title: str) -> None:
    assert parse_dependabot_title(title) is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Update dependency requests to v2.32.3", ("requests", None, "2.32.3")),
        ("chore(deps): update dependency requests to v2.32.3", ("requests", None, "2.32.3")),
        ("fix(deps): update dependency requests to 2.32.3", ("requests", None, "2.32.3")),
        ("Update requests to v2.32.3", ("requests", None, "2.32.3")),
        (
            "Update dependency python-dateutil to v2.9.0",
            ("python-dateutil", None, "2.9.0"),
        ),
        (
            "Update dependency requests from 2.31.0 to v2.32.3",
            ("requests", "2.31.0", "2.32.3"),
        ),
    ],
)
def test_parse_renovate_title(title: str, expected: tuple) -> None:
    parsed = parse_renovate_title(title)
    assert parsed is not None
    assert (parsed.name, parsed.old_version, parsed.new_version) == expected
    assert parsed.bot == "renovate"


@pytest.mark.parametrize(
    "title",
    [
        "Update all non-major dependencies",
        "Update dependencies requests and urllib3 to v2.32.0",
        "Update requests",
        "Pin dependency requests to v2.32.3",
    ],
)
def test_parse_renovate_title_rejects(title: str) -> None:
    assert parse_renovate_title(title) is None


@pytest.mark.parametrize(
    ("title", "bot", "expected"),
    [
        ("Bump requests from 2.31.0 to 2.32.0", "dependabot", "requests"),
        ("Update dependency requests to v2.32.3", "renovate", "requests"),
        ("Bump the pip group across 1 directory with 3 updates", "dependabot", None),
        ("Bump the Django group", "dependabot", None),
        ("Update all non-major dependencies", "renovate", None),
        ("Update dependencies requests and urllib3 to v2.32.0", "renovate", None),
        ("Bump requests from 2.31.0 to 2.32.0", "renovate", None),
        ("Update requests", "renovate", None),
    ],
)
def test_classify_title(title: str, bot: str, expected: str | None) -> None:
    result = classify_title(title, bot)  # type: ignore[arg-type]
    assert (result.parsed.name if result.parsed else None) == expected
    if expected is None:
        assert result.reason in {"group_bump", "not_single_package"}


def test_classify_title_reasons() -> None:
    assert classify_title("Bump the pip group", "dependabot").reason == "group_bump"
    assert classify_title("Update all minor dependencies", "renovate").reason == "group_bump"
    assert classify_title("Update requests", "renovate").reason == "not_single_package"


@pytest.mark.parametrize(
    ("title_version", "bump_version", "expected"),
    [
        ("2", "2.1.3", True),
        ("v2", "2.1.3", True),
        ("2.1", "2.1.3", True),
        ("2.1.3", "2.1.3", True),
        ("2.2", "2.1.3", False),
        ("2.1.3.4", "2.1.3", False),
        ("2", None, False),
        (">=2", "2.1.3", False),
    ],
)
def test_loose_version_match(title_version: str, bump_version: str | None, expected: bool) -> None:
    assert loose_version_match(title_version, bump_version) is expected
