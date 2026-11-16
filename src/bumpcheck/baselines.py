"""Simple baseline policies for comparing dependency bumps."""

from __future__ import annotations

from packaging.version import InvalidVersion, Version

from bumpcheck.models import Bump
from bumpcheck.verdict import Verdict


def always_safe(bump: Bump) -> Verdict:
    """Treat every bump as safe."""
    return "SAFE"


def semver_rule(bump: Bump) -> Verdict:
    """Review major bumps, plus minor bumps while the major version is zero."""
    if bump.old_version is None or bump.new_version is None:
        return "SAFE"
    try:
        old = Version(bump.old_version)
        new = Version(bump.new_version)
    except InvalidVersion:
        return "SAFE"
    if new.major > old.major:
        return "REVIEW"
    if old.major == 0 and new.major == 0 and new.minor > old.minor:
        return "REVIEW"
    return "SAFE"
