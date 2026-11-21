"""Simple baseline policies for comparing dependency bumps."""

from __future__ import annotations

from packaging.version import InvalidVersion, Version

from bumpcheck.models import Bump
from bumpcheck.verdict import Verdict


def always_safe(bump: Bump) -> Verdict:
    """Treat every bump as safe."""
    return "SAFE"


def semver_rule(
    bump_or_old: Bump | str | None,
    new_version: str | None = None,
) -> Verdict:
    """Review major bumps, plus minor bumps while the major version is zero.

    Accepts either a Bump or an explicit (old_version, new_version) pair.
    """
    if isinstance(bump_or_old, Bump):
        old_version = bump_or_old.old_version
        new_version = bump_or_old.new_version
    else:
        old_version = bump_or_old
    if old_version is None or new_version is None:
        return "SAFE"
    try:
        old = Version(old_version)
        new = Version(new_version)
    except InvalidVersion:
        return "SAFE"
    if new.major > old.major:
        return "REVIEW"
    if old.major == 0 and new.major == 0 and new.minor > old.minor:
        return "REVIEW"
    return "SAFE"
