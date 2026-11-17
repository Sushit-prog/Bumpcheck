"""Frozen models for benchmark candidate mining."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Bot = Literal["dependabot", "renovate"]
CISummary = Literal["all_passed", "some_failed", "none"]
LabelSuggestion = Literal["OK_SUSPECT", "BROKE_SUSPECT", "UNCLEAR"]
BaseComparisonStatus = Literal["preexisting", "regression", "no_base_run"]
LockKind = Literal["uv.lock", "poetry.lock", "requirements"]
Split = Literal["dev", "test"]
ExcludedReason = Literal[
    "group_bump",
    "not_single_package",
    "no_supported_lockfile",
    "parse_mismatch",
    "no_ci",
    "ci_not_attributable",
    "closed_without_ci_signal",
    "other",
]


class CheckRef(BaseModel):
    """One CI check attached to a candidate's head commit."""

    model_config = ConfigDict(frozen=True)

    name: str
    conclusion: str
    html_url: str | None = None


class FailureVsBase(BaseModel):
    """A failing head check compared against the same check at the base commit."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: BaseComparisonStatus


class FailureExcerpt(BaseModel):
    """Matching lines pulled from a failed check's job log."""

    model_config = ConfigDict(frozen=True)

    check_name: str
    lines: list[str] = Field(default_factory=list)


class LaterFix(BaseModel):
    """A commit that may have fixed a suspected breakage."""

    model_config = ConfigDict(frozen=True)

    sha: str
    message: str
    date: str


class Candidate(BaseModel):
    """A single-package bump PR kept for hand labelling."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    repo: str
    pr_number: int
    bot: Bot
    title: str
    package: str
    old_version: str | None
    new_version: str | None
    base_sha: str
    head_sha: str
    merged: bool
    merged_at: str | None = None
    lockfile_kind: LockKind
    lockfile_paths: list[str]
    ci_checks: list[CheckRef]
    base_ci_checks: list[CheckRef] = Field(default_factory=list)
    ci_summary: CISummary
    failure_vs_base: list[FailureVsBase] = Field(default_factory=list)
    label_suggestion: LabelSuggestion
    failure_excerpts: list[FailureExcerpt] = Field(default_factory=list)
    later_fix_commits: list[LaterFix] = Field(default_factory=list)
    split: Split
    rerun_flaky: bool = False
    label: str | None = None


class Excluded(BaseModel):
    """A PR that was inspected and rejected, with the reason."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    repo: str
    pr_number: int
    reason: ExcludedReason


class Summary(BaseModel):
    """Counts written next to the candidate and exclusion files."""

    model_config = ConfigDict(frozen=True)

    kept: int
    excluded: int
    distinct_repos: int
    labels: dict[str, int]
    reasons: dict[str, int]
    bots: dict[str, int]
    splits: dict[str, int]
    api_calls: int
    cache_hits: int
