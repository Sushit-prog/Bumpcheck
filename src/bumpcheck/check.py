"""Run the full bump-check pipeline: lock diff, API diff, usage trace, verdict."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from bumpcheck.apidiff import ApiDiffResult, diff_versions
from bumpcheck.lockdiff import parse_lock_pair
from bumpcheck.models import Bump, Unresolved
from bumpcheck.rules import decide
from bumpcheck.usage import UsageResult, trace_usage
from bumpcheck.verdict import (
    PIPELINE_VERSION,
    BumpVerdict,
    CheckResult,
    Evidence,
    worst_verdict,
)

DiffFn = Callable[..., ApiDiffResult]
UsageFn = Callable[..., UsageResult | None]


def _parameters(api: ApiDiffResult) -> dict[str, list[tuple[str, str]]]:
    parameters: dict[str, list[tuple[str, str]]] = {}
    for change in api.changes:
        if change.parameter:
            parameters.setdefault(change.symbol, []).append((change.parameter, change.kind))
    return parameters


def _unresolved_verdict(item: Unresolved) -> BumpVerdict:
    rationale = "Package has multiple versions and cannot be compared"
    return BumpVerdict(
        package=item.package,
        old_version=", ".join(item.removed),
        new_version=", ".join(item.added),
        verdict="UNKNOWN",
        rule_id="R0",
        rationale=rationale,
        evidence=(Evidence(kind="reason", summary=rationale, symbol=item.package),),
    )


def _missing_version_verdict(bump: Bump) -> BumpVerdict:
    rationale = "Package was added or removed; no comparable versions"
    return BumpVerdict(
        package=bump.package,
        old_version=bump.old_version or "",
        new_version=bump.new_version or "",
        verdict="UNKNOWN",
        rule_id="R0",
        rationale=rationale,
        evidence=(Evidence(kind="reason", summary=rationale, symbol=bump.package),),
    )


def _error_verdict(bump: Bump, exc: Exception) -> BumpVerdict:
    rationale = f"{type(exc).__name__}: {exc}"
    return BumpVerdict(
        package=bump.package,
        old_version=bump.old_version or "",
        new_version=bump.new_version or "",
        verdict="UNKNOWN",
        rule_id="E1",
        rationale=rationale,
        evidence=(Evidence(kind="reason", summary=rationale),),
        api_status="error",
    )


def check(
    repo_path: Path,
    old_text: str,
    new_text: str,
    kind: str,
    *,
    diff_fn: DiffFn = diff_versions,
    usage_fn: UsageFn = trace_usage,
    cache_dir: Path | None = None,
) -> CheckResult:
    """Check every bump between two lockfiles and classify the breakage risk."""
    parsed = parse_lock_pair(old_text, new_text, kind)
    warnings: list[str] = list(parsed.warnings)
    verdicts: list[BumpVerdict] = [_unresolved_verdict(item) for item in parsed.unresolved]

    for bump in parsed.bumps:
        if bump.old_version is None or bump.new_version is None:
            verdicts.append(_missing_version_verdict(bump))
            continue
        try:
            api = diff_fn(
                bump.package,
                bump.old_version,
                bump.new_version,
                cache_dir=cache_dir,
            )
            warnings.extend(api.warnings)
            usage: UsageResult | None = None
            if api.status == "ok" and api.changes:
                usage = usage_fn(
                    repo_path,
                    bump.package,
                    [change.symbol for change in api.changes],
                    import_names=list(api.top_level),
                    parameters=_parameters(api),
                )
                if usage is not None:
                    warnings.extend(usage.warnings)
            verdicts.append(decide(bump, api, usage))
        except Exception as exc:
            verdicts.append(_error_verdict(bump, exc))

    return CheckResult(
        pipeline_version=PIPELINE_VERSION,
        overall=worst_verdict([verdict.verdict for verdict in verdicts]),
        bumps=tuple(verdicts),
        warnings=tuple(warnings),
    )


def format_text(result: CheckResult) -> str:
    """Render a check result as human-readable text."""
    lines: list[str] = []
    for bump in result.bumps:
        old = bump.old_version or "?"
        new = bump.new_version or "?"
        lines.append(
            f"{bump.verdict} {bump.package} {old}->{new} [{bump.rule_id}] {bump.rationale}"
        )
        for item in bump.evidence:
            if item.file is not None and item.line is not None:
                lines.append(f"  {item.file}:{item.line} {item.summary}")
    lines.append(f"overall: {result.overall}")
    return "\n".join(lines)
