"""Parse Dependabot and Renovate pull request titles.

The Dependabot pattern and the :class:`ParsedTitle` shape are shared with
``scripts/realworld.py``, which re-exports them for its own tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Bot = Literal["dependabot", "renovate"]

_TITLE_RE = re.compile(
    r"^(?:(?P<prefix>[A-Za-z]+)\(deps[^)]*\):?\s+)?"
    r"[Bb]ump\s+(?P<name>\S+)\s+from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+)"
    r"(?:\s+in\s+the\s+(?P<group>\S+)\s+group\s+across\s+\d+\s+director(?:y|ies))?"
    r"(?:\s+in\s+(?P<path>\S+))?\s*$"
)

_RENOVATE_RE = re.compile(
    r"^(?:(?P<prefix>[A-Za-z]+)\(deps[^)]*\):?\s+)?"
    r"[Uu]pdate\s+(?:dependency\s+)?(?P<name>[\w.+-]+?)"
    r"\s+(?:from\s+(?P<old>[\w.+-]+)\s+)?to\s+v?(?P<new>[\w.+-]+)\s*$"
)

_DECORATION_RE = re.compile(r"(?:\s*-\s*autoclosed|\s*\[[^\]]*\]|\s*\([^)]*\))\s*$")

_BOT_UPDATE_RE = re.compile(
    r"^(?:Update\b|Bump\b|Configure\b|(?:chore|fix|build)\(deps\)(?=[\s:(!]|$))",
    re.IGNORECASE,
)
_MERGE_PR_BOT_RE = re.compile(
    r"^Merge pull request #\d+ from (?:[^/\s]+/)?(?:renovate|dependabot)/",
    re.IGNORECASE,
)
_MERGE_BRANCH_BOT_RE = re.compile(r"^Merge branch\b.*\b(?:renovate|dependabot)/", re.IGNORECASE)

_GROUP_MARKERS = (
    re.compile(r"\bbump\s+the\s+.+\s+group\b", re.IGNORECASE),
    re.compile(r"\bwith\s+\d+\s+updates?\b", re.IGNORECASE),
    re.compile(r"\bupdate\s+all\b", re.IGNORECASE),
    re.compile(
        r"\ball\s+(?:non-major\s+|major\s+|minor\s+|patch\s+)?dependencies\b", re.IGNORECASE
    ),
    re.compile(r"\b[\w.+-]+\s+and\s+[\w.+-]+\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class ParsedTitle:
    """A single-package bump parsed out of a pull request title."""

    name: str
    old_version: str | None
    new_version: str
    path: str | None = None
    bot: Bot = "dependabot"
    autoclosed: bool = False


DependabotTitle = ParsedTitle


@dataclass(frozen=True)
class TitleResult:
    """Either a parsed single-package bump or the reason it was rejected."""

    parsed: ParsedTitle | None
    reason: Literal["group_bump", "not_single_package"] | None = None


def is_bot_commit(message: str) -> bool:
    """Return True for dependency-bot commit subjects."""
    subject = message.strip().splitlines()[0] if message.strip() else ""
    return any(
        pattern.search(subject)
        for pattern in (_BOT_UPDATE_RE, _MERGE_PR_BOT_RE, _MERGE_BRANCH_BOT_RE)
    )


def parse_dependabot_title(title: str) -> ParsedTitle | None:
    """Parse a Dependabot bump title, or return None for other titles."""
    match = _TITLE_RE.match(title.strip())
    if match is None:
        return None
    return ParsedTitle(
        name=match.group("name"),
        old_version=match.group("old"),
        new_version=match.group("new"),
        path=match.group("path"),
        bot="dependabot",
    )


def _strip_decorations(title: str) -> tuple[str, bool]:
    """Drop trailing Renovate decorations, never stripping the title to nothing."""
    cleaned = title.strip()
    autoclosed = False
    while True:
        match = _DECORATION_RE.search(cleaned)
        if match is None:
            break
        stripped = cleaned[: match.start()].rstrip()
        if not stripped:
            break
        if "autoclosed" in match.group(0).lower():
            autoclosed = True
        cleaned = stripped
    return cleaned, autoclosed


def parse_renovate_title(title: str) -> ParsedTitle | None:
    """Parse a Renovate bump title, or return None for other titles.

    Renovate usually omits the old version, so ``old_version`` is often None.
    Trailing decorations such as ``- autoclosed``, ``[SECURITY]`` and
    ``(major)`` are stripped before parsing.
    """
    cleaned, autoclosed = _strip_decorations(title)
    match = _RENOVATE_RE.match(cleaned)
    if match is None:
        return None
    return ParsedTitle(
        name=match.group("name"),
        old_version=match.group("old"),
        new_version=match.group("new"),
        bot="renovate",
        autoclosed=autoclosed,
    )


def classify_title(title: str, bot: Bot) -> TitleResult:
    """Classify a PR title as a single-package bump or an exclusion reason."""
    parsed = parse_dependabot_title(title) if bot == "dependabot" else parse_renovate_title(title)
    if parsed is not None:
        return TitleResult(parsed=parsed)
    if any(marker.search(title) for marker in _GROUP_MARKERS):
        return TitleResult(parsed=None, reason="group_bump")
    return TitleResult(parsed=None, reason="not_single_package")


def loose_version_match(title_version: str, bump_version: str | None) -> bool:
    """Match a Renovate version loosely by dotted-segment prefix.

    "v2" matches "2.1.3" and "2" matches "2.0", but "2.1" does not match
    "2.0.9". Dependabot versions are compared exactly instead.
    """
    if bump_version is None:
        return False
    title_parts = [part for part in title_version.strip().lstrip("vV").split(".") if part]
    bump_parts = [part for part in bump_version.strip().lstrip("vV").split(".") if part]
    if not title_parts or len(title_parts) > len(bump_parts):
        return False
    return bump_parts[: len(title_parts)] == title_parts
