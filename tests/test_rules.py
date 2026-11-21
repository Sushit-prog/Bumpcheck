from __future__ import annotations

from pathlib import Path

import pytest

from bumpcheck.apidiff import ApiChange, ApiDiffResult, Location
from bumpcheck.models import Bump
from bumpcheck.projectpython import ProjectPythonSet
from bumpcheck.rules import PythonSupportContext, decide
from bumpcheck.usage import (
    SkippedFile,
    UsageHit,
    UsageResult,
    match_hit_to_change,
    trace_usage,
)

PACKAGE = "pkg"


def make_bump(package: str = PACKAGE) -> Bump:
    return Bump(package=package, old_version="1.0.0", new_version="2.0.0", source="uv.lock")


def make_api(
    changes: list[ApiChange] | None = None,
    *,
    status: str = "ok",
    reason: str | None = None,
    package: str = PACKAGE,
) -> ApiDiffResult:
    return ApiDiffResult(
        package=package,
        old_version="1.0.0",
        new_version="2.0.0",
        status=status,
        reason=reason,
        changes=changes or [],
        top_level=[package],
    )


def make_change(
    kind: str = "OBJECT_REMOVED",
    symbol: str = "pkg.Client",
    *,
    severity: str = "high",
    parameter: str | None = None,
) -> ApiChange:
    return ApiChange(
        kind=kind,
        symbol=symbol,
        severity=severity,
        detail="details",
        parameter=parameter,
        old_location=Location(file="pkg/client.py", line=10),
    )


def make_hit(
    symbol: str = "pkg.Client",
    *,
    kind: str = "call",
    confidence: str = "exact",
    keywords: tuple[str, ...] = (),
    has_star_args: bool = False,
    has_star_kwargs: bool = False,
    file: str = "app.py",
    line: int = 3,
) -> UsageHit:
    return UsageHit(
        symbol=symbol,
        file=file,
        line=line,
        col=0,
        snippet="...",
        kind=kind,
        confidence=confidence,
        keywords=keywords,
        n_positional=0,
        has_star_args=has_star_args,
        has_star_kwargs=has_star_kwargs,
    )


def make_usage(
    hits: list[UsageHit] | None = None,
    *,
    risks: list[str] | None = None,
    package: str = PACKAGE,
    files_scanned: int = 1,
    files_skipped: list[SkippedFile] | None = None,
) -> UsageResult:
    collected = hits or []
    symbols = sorted({hit.symbol for hit in collected}) or [f"{package}.Client"]
    return UsageResult(
        package=package,
        symbols=symbols,
        hits=collected,
        files_scanned=files_scanned,
        files_skipped=files_skipped or [],
        risks=risks or [],
    )


def make_context(
    old: str | None,
    new: str | None,
    *,
    state: str = "available",
    source: str = "ci_matrix",
    status: str = "explicit",
    versions: tuple[str, ...] = ("3.8", "3.9", "3.10"),
    warnings: tuple[str, ...] = (),
) -> PythonSupportContext:
    return PythonSupportContext(
        old_requires_python=old,
        new_requires_python=new,
        state=state,
        project=ProjectPythonSet(
            source=source,
            status=status,
            versions=versions,
            warnings=warnings,
        ),
    )


def test_r1_no_api_diff_is_unknown() -> None:
    result = decide(
        make_bump(),
        make_api(status="no_api_diff", reason="no_wheel"),
        make_usage(),
    )

    assert result.verdict == "UNKNOWN"
    assert result.rule_id == "R1"
    assert "no_wheel" in result.rationale


def test_r2_no_changes_is_safe() -> None:
    result = decide(make_bump(), make_api(), make_usage())

    assert result.verdict == "SAFE"
    assert result.rule_id == "R2"


def test_r3_changes_without_usage_is_unknown() -> None:
    result = decide(make_bump(), make_api([make_change()]), None)

    assert result.verdict == "UNKNOWN"
    assert result.rule_id == "R3"


@pytest.mark.parametrize(
    ("change", "hit", "expected"),
    [
        (
            make_change("OBJECT_REMOVED", "pkg.Client"),
            make_hit("pkg.Client"),
            "LIKELY_BREAKING",
        ),
        (
            make_change("OBJECT_CHANGED_KIND", "pkg.Client"),
            make_hit("pkg.Client", kind="attribute"),
            "LIKELY_BREAKING",
        ),
        (
            make_change("PARAMETER_REMOVED", "pkg.Client", parameter="timeout"),
            make_hit("pkg.Client", keywords=("timeout",)),
            "LIKELY_BREAKING",
        ),
        (
            make_change("PARAMETER_ADDED_REQUIRED", "pkg.Client", parameter="token"),
            make_hit("pkg.Client"),
            "LIKELY_BREAKING",
        ),
        (
            make_change("PARAMETER_CHANGED_KIND", "pkg.Client", parameter="timeout"),
            make_hit("pkg.Client"),
            "REVIEW",
        ),
        (
            make_change("PARAMETER_CHANGED_REQUIRED", "pkg.Client", parameter="timeout"),
            make_hit("pkg.Client"),
            "REVIEW",
        ),
        (
            make_change("PARAMETER_MOVED", "pkg.Client", parameter="timeout"),
            make_hit("pkg.Client"),
            "REVIEW",
        ),
    ],
)
def test_r4_exact_usage_branches(change: ApiChange, hit: UsageHit, expected: str) -> None:
    result = decide(make_bump(), make_api([change]), make_usage([hit]))

    assert result.verdict == expected
    assert result.rule_id == "R4"
    assert any(item.kind == "api_change" for item in result.evidence)
    assert any(item.kind == "usage_hit" for item in result.evidence)


@pytest.mark.parametrize(
    ("change", "hit"),
    [
        (
            make_change("PARAMETER_REMOVED", "pkg.Client", parameter="timeout"),
            make_hit("pkg.Client"),
        ),
        (
            make_change(
                "PARAMETER_CHANGED_DEFAULT", "pkg.Client", severity="medium", parameter="timeout"
            ),
            make_hit("pkg.Client"),
        ),
        (
            make_change("OBJECT_REMOVED", "pkg.Client"),
            make_hit("pkg.Client", confidence="possible"),
        ),
        (
            make_change("PARAMETER_ADDED_REQUIRED", "pkg.Client", parameter="token"),
            make_hit("pkg.Client", has_star_args=True),
        ),
        (
            make_change("PARAMETER_ADDED_REQUIRED", "pkg.Client", parameter="token"),
            make_hit("pkg.Client", has_star_kwargs=True),
        ),
        (
            make_change("PARAMETER_ADDED_REQUIRED", "pkg.Client", parameter="token"),
            make_hit("pkg.Client", keywords=("token",)),
        ),
        (
            make_change("PARAMETER_ADDED_REQUIRED", "pkg.Client", parameter="token"),
            make_hit("pkg.Client", kind="import"),
        ),
        (
            make_change("ATTRIBUTE_CHANGED_TYPE", "pkg.Client", severity="low"),
            make_hit("pkg.Client", kind="attribute"),
        ),
    ],
)
def test_r5_other_matching_hits_are_review(change: ApiChange, hit: UsageHit) -> None:
    result = decide(make_bump(), make_api([change]), make_usage([hit]))

    assert result.verdict == "REVIEW"
    assert result.rule_id == "R5"


@pytest.mark.parametrize(
    "risk",
    [
        "star_import",
        "dynamic_getattr",
        "dynamic_import",
        "scan_truncated",
        "method_target_untraced",
        "unparsable_file",
    ],
)
def test_r6_blocking_risks_are_review(risk: str) -> None:
    result = decide(
        make_bump(),
        make_api([make_change("OBJECT_REMOVED", "pkg.Client")]),
        make_usage(risks=[risk]),
    )

    assert result.verdict == "REVIEW"
    assert result.rule_id == "R6"
    assert result.risks == (risk,)


def test_dynamic_import_unknown_target_alone_stays_safe() -> None:
    result = decide(
        make_bump(),
        make_api([make_change("OBJECT_REMOVED", "pkg.Client")]),
        make_usage(risks=["dynamic_import_unknown_target"]),
    )

    assert result.verdict == "SAFE"
    assert result.rule_id == "R7"
    assert any(
        item.kind == "risk" and item.summary == "dynamic_import_unknown_target"
        for item in result.evidence
    )


def test_r7_no_matching_hits_is_safe() -> None:
    result = decide(
        make_bump(),
        make_api([make_change("OBJECT_REMOVED", "pkg.Client")]),
        make_usage(),
    )

    assert result.verdict == "SAFE"
    assert result.rule_id == "R7"
    assert any(item.symbol == "pkg.Client" for item in result.evidence)


def test_bare_import_does_not_match_descendant_change(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("import idna\n", encoding="utf-8")

    usage = trace_usage(tmp_path, "idna", ["idna.core.valid_string_length"])
    result = decide(
        make_bump("idna"),
        make_api(
            [make_change("OBJECT_CHANGED_KIND", "idna.core.valid_string_length")],
            package="idna",
        ),
        usage,
    )

    assert result.verdict == "SAFE"
    assert result.rule_id == "R7"


def test_ancestor_call_matches_init_change_in_rules() -> None:
    change = make_change("PARAMETER_ADDED_REQUIRED", "pkg.Client.__init__", parameter="token")

    result = decide(make_bump(), make_api([change]), make_usage([make_hit("pkg.Client")]))

    assert result.verdict == "LIKELY_BREAKING"
    assert result.rule_id == "R4"


def test_call_matches_init_parameter_change(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("from pkg import Client\nClient()\n", encoding="utf-8")

    usage = trace_usage(tmp_path, "pkg", ["pkg.Client.__init__"])
    call = next(hit for hit in usage.hits if hit.kind == "call")

    assert match_hit_to_change(call, "pkg.Client.__init__") == "ancestor_call"


def test_class_import_does_not_match_method_change(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("from pkg import Client\n", encoding="utf-8")

    usage = trace_usage(tmp_path, "pkg", ["pkg.Client.method"])
    import_hit = next(hit for hit in usage.hits if hit.kind == "import")

    assert match_hit_to_change(import_hit, "pkg.Client.method") is None
    result = decide(
        make_bump(),
        make_api([make_change("OBJECT_REMOVED", "pkg.Client.method")]),
        usage,
    )
    assert result.verdict == "SAFE"
    assert result.rule_id == "R7"


def test_match_hit_to_change_reports_relations() -> None:
    exact = make_hit("pkg.Client")
    descendant = make_hit("pkg.Client.method")
    ancestor = make_hit("pkg.Client")
    fallback = make_hit("pkg.Client", kind="import", confidence="possible")

    assert match_hit_to_change(exact, "pkg.Client") == "exact"
    assert match_hit_to_change(descendant, "pkg.Client") == "descendant"
    assert match_hit_to_change(ancestor, "pkg.Client.method") == "ancestor_call"
    assert match_hit_to_change(fallback, "pkg.sub.Client") == "fallback"


def test_r8_tightened_with_ci_matrix_exclusion_is_likely_breaking() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(">=3.8", ">=3.10", versions=("3.8", "3.9", "3.10")),
    )
    assert result.verdict == "LIKELY_BREAKING"
    assert result.rule_id == "R8"


def test_r8_tightened_with_declared_range_is_review() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(
            ">=3.8",
            ">=3.10",
            source="pyproject",
            status="declared",
            versions=("3.9", "3.10"),
        ),
    )
    assert result.verdict == "REVIEW"
    assert result.rule_id == "R8"


def test_r8_tightened_with_unknown_project_set_is_unknown() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(">=3.8", ">=3.10", status="unknown", versions=()),
    )
    assert result.verdict == "UNKNOWN"
    assert result.rule_id == "R8"


def test_r8_loosened_spec_does_not_fire() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(">=3.10", ">=3.8"),
    )
    assert result.verdict == "SAFE"
    assert result.rule_id == "R2"

    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(">=3.10", None),
    )
    assert result.verdict == "SAFE"
    assert result.rule_id == "R2"


def test_r8_unchanged_spec_does_not_fire() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(">=3.9", ">=3.9"),
    )
    assert result.verdict == "SAFE"
    assert result.rule_id == "R2"


@pytest.mark.parametrize(
    ("old", "new", "expected_excluded"),
    [
        (">=3.8,<3.10", ">=3.10", ("3.8", "3.9")),
        ("~=3.9", ">=3.10,<4", ("3.9",)),
        (">=3.8", ">=3.8,<3.12", ("3.12", "3.13", "3.14")),
    ],
)
def test_r8_specifier_edge_cases(old: str, new: str, expected_excluded: tuple[str, ...]) -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(old, new, source="pyproject", status="declared"),
    )
    assert result.verdict == "REVIEW"
    assert result.rule_id == "R8"
    excluded = next(
        item.summary for item in result.evidence if item.summary.startswith("excluded Python")
    )
    assert excluded == f"excluded Python versions: {', '.join(expected_excluded)}"


def test_r8_precedes_r2_safe() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(">=3.8", ">=3.11"),
    )
    assert result.verdict == "LIKELY_BREAKING"
    assert result.rule_id == "R8"


def test_r8_precedes_r7_safe() -> None:
    result = decide(
        make_bump(),
        make_api([make_change("OBJECT_REMOVED", "pkg.Removed")]),
        make_usage(),
        make_context(">=3.8", ">=3.10"),
    )
    assert result.verdict == "LIKELY_BREAKING"
    assert result.rule_id == "R8"


def test_r8_evidence_includes_specs_source_and_excluded_versions() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(">=3.8", ">=3.10", versions=("3.8", "3.9")),
    )
    summaries = [item.summary for item in result.evidence]
    assert "requires_python old: >=3.8" in summaries
    assert "requires_python new: >=3.10" in summaries
    assert "project Python source: ci_matrix" in summaries
    assert "excluded Python versions: 3.8, 3.9" in summaries


def test_r8_null_requires_python_both_sides_does_not_fire() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(None, None),
    )
    assert result.verdict == "SAFE"
    assert result.rule_id == "R2"


def test_r8_old_null_new_set_is_tightened() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(None, ">=3.10", versions=("3.8",)),
    )
    assert result.verdict == "LIKELY_BREAKING"
    assert result.rule_id == "R8"


def test_r8_new_null_is_loosened() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(">=3.10", None),
    )
    assert result.verdict == "SAFE"
    assert result.rule_id == "R2"


def test_r8_unavailable_metadata_is_unknown() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(">=3.8", ">=3.10", state="unavailable"),
    )
    assert result.verdict == "UNKNOWN"
    assert result.rule_id == "R8"


def test_r8_missing_requires_python_metadata_is_unknown() -> None:
    result = decide(
        make_bump(),
        make_api(),
        make_usage(),
        make_context(None, None, state="unavailable"),
    )
    assert result.verdict == "UNKNOWN"
    assert result.rule_id == "R8"


def test_r8_likely_breaking_outranks_r5_review() -> None:
    result = decide(
        make_bump(),
        make_api([make_change()]),
        make_usage([make_hit(confidence="possible")]),
        make_context(">=3.8", ">=3.10"),
    )
    assert result.verdict == "LIKELY_BREAKING"
    assert result.rule_id == "R8"
    assert any(item.summary.startswith("requires_python") for item in result.evidence)


def test_r8_evidence_attached_when_other_rule_fires() -> None:
    result = decide(
        make_bump(),
        make_api([make_change()]),
        make_usage([make_hit(confidence="possible")]),
        make_context(">=3.8", ">=3.10", source="pyproject", status="declared"),
    )
    assert result.verdict == "REVIEW"
    assert result.rule_id == "R5"
    assert any(item.summary == "requires_python old: >=3.8" for item in result.evidence)


def test_r8_python_floor_bump_with_unused_api_changes_is_likely_breaking() -> None:
    changes = [make_change("OBJECT_REMOVED", f"pkg.Removed{index}") for index in range(3)]
    result = decide(
        make_bump(),
        make_api(changes),
        make_usage(),
        make_context(">=3.8", ">=3.10"),
    )
    assert result.verdict == "LIKELY_BREAKING"
    assert result.rule_id == "R8"
    assert any("3.8" in item.summary for item in result.evidence)


def test_r9_zero_files_scanned_r7_is_unknown() -> None:
    result = decide(
        make_bump(),
        make_api([make_change("OBJECT_REMOVED", "pkg.Removed")]),
        make_usage(
            files_scanned=0,
            files_skipped=[SkippedFile(file="app.py", reason="unreadable")],
        ),
    )
    assert result.verdict == "UNKNOWN"
    assert result.rule_id == "R9"
    assert any("files_scanned=0" in item.summary for item in result.evidence)
    assert any("files_skipped=1" in item.summary for item in result.evidence)


def test_r2_zero_scan_does_not_trigger_r9() -> None:
    result = decide(make_bump(), make_api(), make_usage(files_scanned=0))
    assert result.verdict == "SAFE"
    assert result.rule_id == "R2"
