"""Models for BumpCheck verdicts."""

from __future__ import annotations

from typing import Literal

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, field_validator

Verdict = Literal["SAFE", "REVIEW", "LIKELY_BREAKING", "UNKNOWN"]
EvidenceKind = Literal["api_change", "usage_hit", "risk", "reason"]

PIPELINE_VERSION = "0.1.0-rules1"

SEVERITY_RANK: dict[Verdict, int] = {
    "SAFE": 0,
    "UNKNOWN": 1,
    "REVIEW": 2,
    "LIKELY_BREAKING": 3,
}


def worst_verdict(verdicts: tuple[Verdict, ...] | list[Verdict]) -> Verdict:
    """Return the most severe verdict, treating an empty sequence as SAFE."""
    if not verdicts:
        return "SAFE"
    return max(verdicts, key=lambda verdict: SEVERITY_RANK[verdict])


def at_or_above(verdict: Verdict, threshold: Verdict) -> bool:
    """Return whether ``verdict`` is at least as severe as ``threshold``."""
    return SEVERITY_RANK[verdict] >= SEVERITY_RANK[threshold]


class Evidence(BaseModel):
    """One piece of evidence supporting a verdict."""

    model_config = ConfigDict(frozen=True)

    kind: EvidenceKind
    summary: str
    file: str | None = None
    line: int | None = None
    symbol: str | None = None


class BumpVerdict(BaseModel):
    """The verdict for a single dependency bump."""

    model_config = ConfigDict(frozen=True)

    package: str
    old_version: str
    new_version: str
    verdict: Verdict
    rule_id: str
    rationale: str
    evidence: tuple[Evidence, ...] = ()
    api_status: str | None = None
    risks: tuple[str, ...] = ()

    @field_validator("package")
    @classmethod
    def _canonicalize_package(cls, value: str) -> str:
        return canonicalize_name(value)

    @field_validator("evidence")
    @classmethod
    def _sort_evidence(cls, value: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.kind,
                    item.summary,
                    item.file or "",
                    item.line or 0,
                    item.symbol or "",
                ),
            )
        )

    @field_validator("risks")
    @classmethod
    def _sort_risks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class CheckResult(BaseModel):
    """The verdicts for every bump between two lockfiles."""

    model_config = ConfigDict(frozen=True)

    pipeline_version: str = PIPELINE_VERSION
    overall: Verdict
    bumps: tuple[BumpVerdict, ...] = Field(default_factory=tuple)
    warnings: tuple[str, ...] = ()

    @field_validator("bumps")
    @classmethod
    def _sort_bumps(cls, value: tuple[BumpVerdict, ...]) -> tuple[BumpVerdict, ...]:
        return tuple(
            sorted(
                value,
                key=lambda bump: (
                    bump.package,
                    bump.old_version,
                    bump.new_version,
                    bump.rule_id,
                ),
            )
        )

    @field_validator("warnings")
    @classmethod
    def _sort_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))
