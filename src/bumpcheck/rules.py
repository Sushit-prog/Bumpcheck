"""Pure verdict rules for dependency bumps."""

from __future__ import annotations

from bumpcheck.apidiff import ApiChange, ApiDiffResult
from bumpcheck.models import Bump
from bumpcheck.usage import MatchKind, UsageHit, UsageResult, match_hit_to_change
from bumpcheck.verdict import BumpVerdict, Evidence, Verdict

_BLOCKING_RISKS = frozenset(
    {
        "star_import",
        "dynamic_getattr",
        "dynamic_import",
        "scan_truncated",
        "method_target_untraced",
        "unparsable_file",
    }
)
_REMOVAL_KINDS = frozenset({"OBJECT_REMOVED", "OBJECT_CHANGED_KIND"})
_SPECIAL_PARAMETER_KINDS = frozenset({"PARAMETER_REMOVED", "PARAMETER_ADDED_REQUIRED"})


def _change_evidence(change: ApiChange) -> Evidence:
    summary = f"{change.kind} {change.symbol}"
    if change.parameter:
        summary = f"{summary} ({change.parameter})"
    location = change.old_location
    return Evidence(
        kind="api_change",
        summary=summary,
        file=location.file if location is not None else None,
        line=location.line if location is not None else None,
        symbol=change.symbol,
    )


def _hit_evidence(hit: UsageHit) -> Evidence:
    return Evidence(
        kind="usage_hit",
        summary=f"{hit.kind} {hit.symbol}",
        file=hit.file,
        line=hit.line,
        symbol=hit.symbol,
    )


def _matching_hits(change: ApiChange, usage: UsageResult) -> list[tuple[UsageHit, MatchKind]]:
    matches: list[tuple[UsageHit, MatchKind]] = []
    for hit in usage.hits:
        match = match_hit_to_change(hit, change.symbol)
        if match is not None:
            matches.append((hit, match))
    return matches


def _r4_verdict(change: ApiChange, hit: UsageHit) -> Verdict | None:
    if hit.confidence != "exact":
        return None
    if change.kind in _REMOVAL_KINDS:
        return "LIKELY_BREAKING"
    if change.kind == "PARAMETER_REMOVED":
        if change.parameter is not None and change.parameter in hit.keywords:
            return "LIKELY_BREAKING"
        return None
    if change.kind == "PARAMETER_ADDED_REQUIRED":
        if (
            hit.kind == "call"
            and change.parameter is not None
            and change.parameter not in hit.keywords
            and not hit.has_star_args
            and not hit.has_star_kwargs
        ):
            return "LIKELY_BREAKING"
        return None
    if (
        change.kind.startswith("PARAMETER_")
        and change.kind not in _SPECIAL_PARAMETER_KINDS
        and change.severity == "high"
    ):
        return "REVIEW"
    return None


def _result(
    bump: Bump,
    api: ApiDiffResult,
    usage: UsageResult | None,
    verdict: Verdict,
    rule_id: str,
    rationale: str,
    evidence: tuple[Evidence, ...] = (),
) -> BumpVerdict:
    return BumpVerdict(
        package=bump.package,
        old_version=bump.old_version or "",
        new_version=bump.new_version or "",
        verdict=verdict,
        rule_id=rule_id,
        rationale=rationale,
        evidence=evidence,
        api_status=api.status,
        risks=tuple(sorted(usage.risks)) if usage is not None else (),
    )


def decide(
    bump: Bump,
    api: ApiDiffResult,
    usage: UsageResult | None,
) -> BumpVerdict:
    """Decide the verdict for one bump. The first matching rule wins."""
    if api.status == "no_api_diff":
        reason = api.reason or "unknown reason"
        return _result(
            bump,
            api,
            usage,
            "UNKNOWN",
            "R1",
            f"API diff unavailable: {reason}",
            (Evidence(kind="reason", summary=f"API diff unavailable: {reason}"),),
        )

    if not api.changes:
        return _result(
            bump,
            api,
            usage,
            "SAFE",
            "R2",
            "No API changes between the two versions",
            (Evidence(kind="reason", summary="No API changes between the two versions"),),
        )

    if usage is None:
        return _result(
            bump,
            api,
            usage,
            "UNKNOWN",
            "R3",
            "API changes exist but usage was not traced",
            (Evidence(kind="reason", summary="Usage tracing was not run"),),
        )

    r4_review: tuple[ApiChange, list[tuple[UsageHit, MatchKind]]] | None = None
    for change in api.changes:
        matches = _matching_hits(change, usage)
        for hit, _ in matches:
            verdict = _r4_verdict(change, hit)
            if verdict == "LIKELY_BREAKING":
                evidence = (_change_evidence(change), *(_hit_evidence(item) for item, _ in matches))
                rationale = f"Exact usage matches {change.kind} on {change.symbol}"
                return _result(bump, api, usage, "LIKELY_BREAKING", "R4", rationale, evidence)
            if verdict == "REVIEW" and r4_review is None:
                r4_review = (change, matches)
    if r4_review is not None:
        change, matches = r4_review
        evidence = (_change_evidence(change), *(_hit_evidence(item) for item, _ in matches))
        rationale = f"High-severity {change.kind} on {change.symbol} has exact usage"
        return _result(bump, api, usage, "REVIEW", "R4", rationale, evidence)

    matched_changes = [
        (change, matches) for change in api.changes if (matches := _matching_hits(change, usage))
    ]
    if matched_changes:
        evidence_list: list[Evidence] = []
        for change, matches in matched_changes:
            evidence_list.append(_change_evidence(change))
            evidence_list.extend(_hit_evidence(hit) for hit, _ in matches)
        return _result(
            bump,
            api,
            usage,
            "REVIEW",
            "R5",
            "A usage may be affected by the API changes",
            tuple(evidence_list),
        )

    blocking = sorted(set(usage.risks) & _BLOCKING_RISKS)
    if blocking:
        return _result(
            bump,
            api,
            usage,
            "REVIEW",
            "R6",
            "No matching usages, but risks prevent a SAFE verdict: " + ", ".join(blocking),
            tuple(Evidence(kind="risk", summary=risk) for risk in blocking),
        )

    evidence_list = [
        Evidence(
            kind="reason",
            summary=f"No usage found for {change.symbol}",
            symbol=change.symbol,
        )
        for change in api.changes
    ]
    if "dynamic_import_unknown_target" in usage.risks:
        evidence_list.append(Evidence(kind="risk", summary="dynamic_import_unknown_target"))
    return _result(
        bump,
        api,
        usage,
        "SAFE",
        "R7",
        "No matching usages and no blocking risks",
        tuple(evidence_list),
    )
