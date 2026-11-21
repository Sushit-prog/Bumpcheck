"""Run the full bump-check pipeline: lock diff, API diff, usage trace, verdict."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from bumpcheck.apidiff import ApiDiffResult, diff_versions
from bumpcheck.lockdiff import parse_lock_pair
from bumpcheck.models import Bump, Unresolved
from bumpcheck.projectpython import ProjectPythonSet, resolve_project_python
from bumpcheck.rules import PythonSupportContext, decide
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
ProjectPythonFn = Callable[[Path], ProjectPythonSet]
BumpDiagnosticHook = Callable[
    [BumpVerdict, ApiDiffResult | None, UsageResult | None],
    None,
]


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
    project_python_fn: ProjectPythonFn = resolve_project_python,
    cache_dir: Path | None = None,
    on_bump: BumpDiagnosticHook | None = None,
) -> CheckResult:
    """Check every bump between two lockfiles and classify the breakage risk."""
    parsed = parse_lock_pair(old_text, new_text, kind)
    warnings: list[str] = list(parsed.warnings)
    try:
        project_python = project_python_fn(repo_path)
    except Exception as exc:
        project_python = ProjectPythonSet(
            source="unknown",
            status="unknown",
            warnings=(f"project Python resolution failed: {type(exc).__name__}: {exc}",),
        )
    warnings.extend(project_python.warnings)

    def emit(
        verdict: BumpVerdict,
        api: ApiDiffResult | None,
        usage: UsageResult | None,
    ) -> None:
        if on_bump is not None:
            on_bump(verdict, api, usage)

    verdicts: list[BumpVerdict] = []
    for item in parsed.unresolved:
        verdict = _unresolved_verdict(item)
        verdicts.append(verdict)
        emit(verdict, None, None)

    for bump in parsed.bumps:
        if bump.old_version is None or bump.new_version is None:
            verdict = _missing_version_verdict(bump)
            verdicts.append(verdict)
            emit(verdict, None, None)
            continue
        api: ApiDiffResult | None = None
        usage: UsageResult | None = None
        try:
            api = diff_fn(
                bump.package,
                bump.old_version,
                bump.new_version,
                cache_dir=cache_dir,
            )
            warnings.extend(api.warnings)
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
            state = (
                "unavailable"
                if api.old_requires_python_state == "unavailable"
                or api.new_requires_python_state == "unavailable"
                else "available"
            )
            python_support = PythonSupportContext(
                old_requires_python=api.old_requires_python,
                new_requires_python=api.new_requires_python,
                state=state,
                project=project_python,
            )
            verdict = decide(bump, api, usage, python_support)
            verdicts.append(verdict)
            emit(verdict, api, usage)
        except Exception as exc:
            verdict = _error_verdict(bump, exc)
            verdicts.append(verdict)
            emit(verdict, api, None)

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
            location = (
                f"{item.file}:{item.line} "
                if item.file is not None and item.line is not None
                else ""
            )
            lines.append(f"  {location}{item.summary}")
    lines.append(f"overall: {result.overall}")
    return "\n".join(lines)


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def format_markdown(result: CheckResult) -> str:
    """Render a check result as a compact Markdown report for pull requests."""
    lines = [f"## BumpCheck: {result.overall}", "", "Overall verdict: `" + result.overall + "`"]
    if result.bumps:
        lines.extend(
            [
                "",
                "| Package | Change | Verdict | Rule | Rationale |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for bump in result.bumps:
            old = bump.old_version or "?"
            new = bump.new_version or "?"
            lines.append(
                "| `{package}` | `{old}` → `{new}` | {verdict} | {rule} | {rationale} |".format(
                    package=_md_cell(bump.package),
                    old=_md_cell(old),
                    new=_md_cell(new),
                    verdict=bump.verdict,
                    rule=bump.rule_id,
                    rationale=_md_cell(bump.rationale),
                )
            )
    else:
        lines.extend(["", "No comparable dependency bumps found."])

    evidence_lines = [item for bump in result.bumps for item in bump.evidence]
    if evidence_lines:
        lines.append("")
        lines.append("Evidence:")
        for item in evidence_lines:
            location = f"`{item.file}:{item.line}`" if item.file is not None and item.line else None
            label = f"{location} — " if location else ""
            lines.append(f"- {label}{_md_cell(item.summary)}")

    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {_md_cell(warning)}" for warning in result.warnings)

    lines.extend(
        [
            "",
            "> SAFE means no relevant static API changes or direct usages were found. "
            "It does not prove runtime or behavioral compatibility.",
        ]
    )
    return "\n".join(lines)
