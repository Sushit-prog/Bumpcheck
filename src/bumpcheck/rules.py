"""Pure verdict rules for dependency bumps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from bumpcheck.apidiff import ApiChange, ApiDiffResult
from bumpcheck.models import Bump, DependencyPin
from bumpcheck.projectpython import ProjectPythonSet
from bumpcheck.pycompat import MetadataState, assess_requires_python_change
from bumpcheck.usage import MatchKind, UsageHit, UsageResult, match_hit_to_change
from bumpcheck.verdict import SEVERITY_RANK, BumpVerdict, Evidence, Verdict

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


@dataclass(frozen=True)
class PythonSupportContext:
    old_requires_python: str | None
    new_requires_python: str | None
    state: MetadataState
    project: ProjectPythonSet


@dataclass(frozen=True)
class DependencyConflictContext:
    new_requires_dist: tuple[str, ...]
    pins: tuple[DependencyPin, ...]
    project: ProjectPythonSet


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


def _specifier_text(value: str | None) -> str:
    return value if value is not None else "null (unconstrained)"


def _python_support_evidence(
    assessment: Any,
    project: ProjectPythonSet,
) -> tuple[Evidence, ...]:
    summaries = [
        f"requires_python old: {_specifier_text(assessment.old)}",
        f"requires_python new: {_specifier_text(assessment.new)}",
        f"requires_python metadata: {assessment.state}",
        f"project Python source: {project.source}",
        f"project Python versions: {', '.join(project.versions) or 'unknown'}",
        f"excluded Python versions: {', '.join(assessment.excluded) or 'none'}",
    ]
    summaries.extend(assessment.warnings)
    summaries.extend(project.warnings)
    return tuple(Evidence(kind="reason", summary=summary) for summary in summaries)


def _python_support_verdict(
    bump: Bump,
    api: ApiDiffResult,
    usage: UsageResult | None,
    context: PythonSupportContext | None,
) -> BumpVerdict | None:
    if context is None:
        return None
    assessment = assess_requires_python_change(
        context.old_requires_python,
        context.new_requires_python,
        context.project.versions,
        state=context.state,
    )
    evidence = _python_support_evidence(assessment, context.project)
    if not assessment.decided:
        return _result(
            bump,
            api,
            usage,
            "UNKNOWN",
            "R8",
            "Python support change could not be evaluated",
            evidence,
        )
    if not assessment.tightened:
        return None
    if context.project.status == "unknown":
        return _result(
            bump,
            api,
            usage,
            "UNKNOWN",
            "R8",
            "requires_python was tightened but the project Python set is unknown",
            evidence,
        )
    if context.project.status == "declared":
        return _result(
            bump,
            api,
            usage,
            "REVIEW",
            "R8",
            "requires_python tightens Python support declared by the project",
            evidence,
        )
    excluded = set(assessment.excluded)
    if not any(version in excluded for version in context.project.versions):
        return None
    return _result(
        bump,
        api,
        usage,
        "LIKELY_BREAKING",
        "R8",
        "requires_python excludes a Python version used by the project",
        evidence,
    )


def _zero_scan_verdict(
    bump: Bump,
    api: ApiDiffResult,
    usage: UsageResult | None,
) -> BumpVerdict | None:
    files_scanned = usage.files_scanned if usage is not None else 0
    files_skipped = len(usage.files_skipped) if usage is not None else 0
    if files_scanned > 0:
        return None
    summary = f"files_scanned={files_scanned}; files_skipped={files_skipped}"
    return _result(
        bump,
        api,
        usage,
        "UNKNOWN",
        "R9",
        "No Python files were scanned; SAFE requires scan evidence",
        (Evidence(kind="reason", summary=summary),),
    )


def _rebuild_verdict(
    base: BumpVerdict,
    *,
    verdict: Verdict | None = None,
    rule_id: str | None = None,
    rationale: str | None = None,
    evidence: tuple[Evidence, ...],
) -> BumpVerdict:
    return BumpVerdict(
        package=base.package,
        old_version=base.old_version,
        new_version=base.new_version,
        verdict=verdict or base.verdict,
        rule_id=rule_id or base.rule_id,
        rationale=rationale or base.rationale,
        evidence=evidence,
        api_status=base.api_status,
        risks=base.risks,
    )


def _merge_verdicts(base: BumpVerdict, extra: BumpVerdict) -> BumpVerdict:
    if SEVERITY_RANK[extra.verdict] > SEVERITY_RANK[base.verdict]:
        return _rebuild_verdict(
            base,
            verdict=extra.verdict,
            rule_id=extra.rule_id,
            rationale=f"{base.rationale}; {extra.rationale}",
            evidence=(*base.evidence, *extra.evidence),
        )
    return _rebuild_verdict(
        base,
        rationale=f"{base.rationale}; {extra.rationale}",
        evidence=(*base.evidence, *extra.evidence),
    )


def _warn(warnings: list[str] | None, message: str) -> None:
    if warnings is not None:
        warnings.append(message)


def _marker_applies(marker: Marker | None, project: ProjectPythonSet) -> bool | None:
    if marker is None:
        return True
    if project.status == "unknown" or not project.versions:
        return None
    for version_text in project.versions:
        try:
            version = Version(version_text)
        except InvalidVersion:
            return None
        release = version.release
        python_version = ".".join(str(part) for part in release[:2])
        python_full_version = ".".join(str(part) for part in release[:3])
        try:
            applies = marker.evaluate(
                {
                    "python_version": python_version,
                    "python_full_version": python_full_version,
                    "extra": "",
                }
            )
        except Exception:
            return None
        if not applies:
            return False
    return True


def _pin_location(pin: DependencyPin) -> str:
    if pin.file and pin.line is not None:
        return f"{pin.file}:{pin.line}"
    return pin.file or "unknown location"


def _dependency_conflict_verdict(
    bump: Bump,
    api: ApiDiffResult,
    usage: UsageResult | None,
    context: DependencyConflictContext | None,
    warnings: list[str] | None,
) -> BumpVerdict | None:
    if context is None:
        return None
    if not context.new_requires_dist:
        if api.new_requires_python_state == "unavailable":
            _warn(
                warnings,
                "R10: new package metadata is unavailable; dependency conflicts were not checked",
            )
        return None

    pins_by_package: dict[str, list[DependencyPin]] = {}
    for pin in context.pins:
        pins_by_package.setdefault(pin.package, []).append(pin)

    conflicts: list[tuple[str, DependencyPin]] = []
    for raw in sorted(set(context.new_requires_dist)):
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            _warn(warnings, f"R10: could not parse new requirement {raw!r}; skipping it")
            continue
        target = canonicalize_name(requirement.name)
        if target == bump.package or target not in pins_by_package:
            continue
        marker_status = _marker_applies(requirement.marker, context.project)
        if marker_status is not True:
            detail = "unknown project Python set" if marker_status is None else "marker is not true"
            _warn(warnings, f"R10: skipped {raw!r} because its {detail}; target={target}")
            continue
        required_extras = tuple(sorted(canonicalize_name(extra) for extra in requirement.extras))
        if not requirement.specifier:
            continue
        target_pins = pins_by_package[target]
        if len({pin.version for pin in target_pins}) > 1:
            _warn(warnings, f"R10: target={target} has multiple pinned versions; skipping it")
            continue
        for pin in target_pins:
            if required_extras and not set(required_extras).issubset(pin.extras):
                _warn(
                    warnings,
                    f"R10: skipped {raw!r} because extras {', '.join(required_extras)} "
                    f"are not pinned for {target} at {_pin_location(pin)}",
                )
                continue
            if pin.version is None:
                _warn(
                    warnings,
                    f"R10: target={target} at {_pin_location(pin)} "
                    "is not an exact pin; skipping it",
                )
                continue
            try:
                pinned_version = Version(pin.version)
                satisfies = pinned_version in requirement.specifier
            except (InvalidVersion, ValueError):
                _warn(
                    warnings,
                    f"R10: could not evaluate {raw!r} against {target}=={pin.version}; skipping it",
                )
                continue
            if not satisfies:
                conflicts.append((raw, pin))

    if not conflicts:
        return None
    evidence = tuple(
        Evidence(
            kind="reason",
            summary=(
                f"new requirement {raw!r} conflicts with "
                f"{pin.package}=={pin.version} pinned at {_pin_location(pin)}"
            ),
            file=pin.file,
            line=pin.line,
            symbol=pin.package,
        )
        for raw, pin in conflicts
    )
    rationale = "New dependency requirements conflict with pinned versions"
    return _result(bump, api, usage, "LIKELY_BREAKING", "R10", rationale, evidence)


def _finalize(
    result: BumpVerdict,
    bump: Bump,
    api: ApiDiffResult,
    usage: UsageResult | None,
    context: PythonSupportContext | None,
    dependency_context: DependencyConflictContext | None = None,
    warnings: list[str] | None = None,
) -> BumpVerdict:
    python_support = _python_support_verdict(bump, api, usage, context)
    dependency = None
    if result.rule_id != "R4":
        dependency = _dependency_conflict_verdict(bump, api, usage, dependency_context, warnings)
    if result.verdict == "SAFE":
        gates = [item for item in (dependency, python_support) if item is not None]
        if result.rule_id == "R7":
            zero_scan = _zero_scan_verdict(bump, api, usage)
            if zero_scan is not None:
                gates.append(zero_scan)
        if not gates:
            return result
        combined = gates[0]
        for gate in gates[1:]:
            combined = _merge_verdicts(combined, gate)
        return _merge_verdicts(result, combined)
    for gate in (dependency, python_support):
        if gate is not None:
            result = _merge_verdicts(result, gate)
    return result


def _decide_base(
    bump: Bump,
    api: ApiDiffResult,
    usage: UsageResult | None,
) -> BumpVerdict:
    """Decide the base verdict for one bump. The first matching rule wins."""
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


def decide(
    bump: Bump,
    api: ApiDiffResult,
    usage: UsageResult | None,
    python_support: PythonSupportContext | None = None,
    dependency_context: DependencyConflictContext | None = None,
    warnings: list[str] | None = None,
) -> BumpVerdict:
    """Decide the verdict for one bump, applying dependency, Python support and scan gates."""
    result = _decide_base(bump, api, usage)
    return _finalize(
        result,
        bump,
        api,
        usage,
        python_support,
        dependency_context,
        warnings,
    )
