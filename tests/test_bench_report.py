from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bumpcheck.bench import report


def label(
    case_id: str,
    value: str,
    *,
    category: str | None = None,
    round_number: int = 1,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "round": round_number,
        "label": value,
        "break_category": category,
        "evidence_kind": "log_names_package" if value == "BROKE" else None,
        "evidence": (
            "log names package"
            if value == "BROKE"
            else "excluded for test"
            if value == "EXCLUDE"
            else ""
        ),
        "exclude_reason": "not_attributable" if value == "EXCLUDE" else None,
        "labeled_on": "2026-01-01",
    }


def base_dir(tmp_path: Path, labels: tuple[dict[str, Any], ...] = ()) -> Path:
    (tmp_path / "candidates.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "labels.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in labels),
        encoding="utf-8",
    )
    (tmp_path / "excluded.jsonl").write_text(
        '{"case_id":"x","repo":"o/r","pr_number":2,"reason":"parse_mismatch"}\n',
        encoding="utf-8",
    )
    return tmp_path


def fingerprint(value: str) -> dict[str, Any]:
    return {"id": value, "git_sha": value, "dirty": False, "source_sha256": value}


def candidate(
    case_id: str,
    old: str | None,
    new: str | None,
    *,
    split: str = "dev",
    sampled_by: str = "any",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "repo": "o/r",
        "pr_number": int(case_id.rsplit("#", 1)[-1]) if "#" in case_id else 1,
        "package": "pkg",
        "old_version": old,
        "new_version": new,
        "split": split,
        "sampled_by": sampled_by,
    }


def write_candidates(directory: Path, rows: tuple[dict[str, Any], ...] = ()) -> None:
    (directory / "candidates.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in rows),
        encoding="utf-8",
    )


def record(
    case_id: str,
    label_value: str,
    verdict: str,
    *,
    category: str | None = None,
    rules: list[str] | None = None,
    error_kind: str | None = None,
    fp: str = "one",
    split: str = "dev",
    versions: tuple[str | None, str | None] | None = None,
    verdicts: dict[str, str] | None = None,
) -> dict[str, Any]:
    default_verdicts = {
        "bumpcheck": verdict,
        "always_safe": "SAFE",
        "semver_rule": "REVIEW",
    }
    diagnostics: dict[str, Any] = {"bumps": []}
    if versions is not None:
        diagnostics["bumps"] = [
            {"old_version": versions[0], "new_version": versions[1], "package": "pkg"}
        ]
    return {
        "case_id": case_id,
        "label": label_value,
        "break_category": category,
        "split": split,
        "sampled_by": "any",
        "fingerprint": fingerprint(fp),
        "verdicts": verdicts if verdicts is not None else default_verdicts,
        "fired_rule_ids": {"bumpcheck": rules or []},
        "evidence_refs": [],
        "error_kind": error_kind,
        "parse_failures": 0,
        "runtime_s": 0.1,
        "diagnostics": diagnostics,
    }


def test_wilson_values() -> None:
    assert report.wilson(0, 0) is None
    low, high = report.wilson(0, 10)
    assert low == 0
    assert round(high, 4) == 0.2775
    low, high = report.wilson(5, 10)
    assert round(low, 4) == 0.2366
    assert round(high, 4) == 0.7634
    low, high = report.wilson(10, 10)
    assert round(low, 4) == 0.7225
    assert high == 1


def test_report_rates_include_wilson_and_unknown_denominators(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (
            label("a", "BROKE", category="api_break"),
            label("b", "BROKE", category="python_version_support"),
            label("c", "OK"),
            label("d", "OK"),
        ),
    )
    records = [
        record("a", "BROKE", "SAFE", category="api_break", rules=["R7"]),
        record(
            "b",
            "BROKE",
            "UNKNOWN",
            category="python_version_support",
            error_kind="no_wheel",
        ),
        record("c", "OK", "SAFE", rules=["R7"]),
        record("d", "OK", "REVIEW"),
    ]

    text = report.render_report(directory, records, split="dev")

    assert text.startswith("# Benchmark report (split: dev)")
    assert "n is small; do not quote these numbers publicly" in text
    known_gap = text.index("## Known gap: api_break breaks")
    assert known_gap < text.index("## Headline metrics")
    assert "o/r#1" not in text
    assert "owner/repo" not in text
    assert "a: BumpCheck said SAFE" in text
    assert "0/2 = 0.0% [0.0, 65.8]" in text
    assert "1/2 = 50.0% [9.5, 90.5]" in text
    assert "1/4 = 25.0% [4.6, 69.9]" in text


def test_report_lists_disputed_and_mixed_fingerprints(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (
            label("a", "BROKE", category="api_break"),
            label("b", "BROKE", category="api_break"),
            label("b", "OK", round_number=2),
        ),
    )
    records = [
        record("a", "BROKE", "SAFE"),
        record("b", "DISPUTED", "UNKNOWN", fp="two"),
    ]

    text = report.render_report(directory, records, split="dev")

    assert "Warning: mixed fingerprints: one, two" in text
    assert "Disputed cases excluded from metrics: b" in text
    assert "round 1 unless round 2 exists" in text
    assert "parse_mismatch=1" in text
    assert "Sampled-by mix: any=0, failure=0, success=0" in text
    assert "Failure-sampled candidates inflate the BROKE share" in text


def test_report_rule_tp_fp_uses_break_category_of_label(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (label("a", "BROKE", category="api_break"), label("b", "OK")),
    )
    records = [
        record("a", "BROKE", "REVIEW", category="api_break", rules=["R7"]),
        record("b", "OK", "SAFE", rules=["R7"]),
    ]

    text = report.render_report(directory, records, split="dev")

    assert "| R7 | 2 | 1 | 1 |" in text


def test_write_report_writes_markdown(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("a", "OK"),))
    records = [record("a", "OK", "SAFE")]

    text = report.write_report(directory, records, split="dev")

    assert text == (directory / "eval" / "report.md").read_text(encoding="utf-8")


def test_report_uses_current_labels_by_case_id(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("a", "BROKE", category="api_break"),))
    records = [record("a", "OK", "SAFE")]

    text = report.render_report(directory, records, split="dev")

    assert "false-safe rate | 1/1 = 100.0%" in text
    assert "Ignored result rows (unlabeled or label/category changed): 1" in text


def test_report_current_exclude_removes_stale_result(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("a", "EXCLUDE"),))
    records = [record("a", "BROKE", "SAFE", category="api_break")]

    text = report.render_report(directory, records, split="dev")

    assert "a: BumpCheck said SAFE" not in text
    assert "false-safe rate | 0/0 (n/a)" in text
    assert "Ignored result rows (unlabeled or label/category changed): 1" in text


def test_report_unlabeled_results_are_ignored(tmp_path: Path) -> None:
    directory = base_dir(tmp_path)
    records = [record("a", "BROKE", "SAFE", category="api_break")]

    text = report.render_report(directory, records, split="dev")

    assert "a: BumpCheck said SAFE" not in text
    assert "Ignored result rows (unlabeled or label/category changed): 1" in text


def test_report_current_disputed_labels_remain_disputed(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (
            label("a", "BROKE", category="api_break"),
            label("a", "OK", round_number=2),
        ),
    )
    records = [record("a", "DISPUTED", "UNKNOWN")]

    text = report.render_report(directory, records, split="dev")

    assert "Disputed cases excluded from metrics: a" in text
    assert "Ignored result rows (unlabeled or label/category changed): 0" in text


def test_report_label_changed_count_is_printed(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("a", "BROKE", category="api_break"),))
    records = [record("a", "OK", "SAFE")]

    text = report.render_report(directory, records, split="dev")

    assert "Ignored result rows (unlabeled or label/category changed): 1" in text


def test_report_error_counts_ignore_none(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (label("a", "BROKE", category="api_break"), label("b", "OK")),
    )
    records = [
        record("a", "BROKE", "SAFE", error_kind=None),
        record("b", "OK", "SAFE", error_kind="timeout"),
    ]

    text = report.render_report(directory, records, split="dev")

    assert "timeout=1" in text
    assert "none=" not in text


def test_report_errors_line_uses_scored_results_only(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (
            label("a", "BROKE", category="api_break"),
            label("b", "OK"),
            label("c", "BROKE", category="api_break"),
            label("c", "OK", round_number=2),
        ),
    )
    records = [
        record("a", "BROKE", "UNKNOWN", error_kind="timeout"),
        record("b", "OK", "SAFE"),
        record("c", "DISPUTED", "UNKNOWN", error_kind="timeout"),
    ]

    text = report.render_report(directory, records, split="dev")

    assert "errors: 2 of 2 evaluated" in text


def test_report_known_gap_has_no_hardcoded_case_id(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("x", "BROKE", category="python_version_support"),))
    records = [record("x", "BROKE", "SAFE", category="python_version_support", rules=["R7"])]

    text = report.render_report(directory, records, split="dev")

    assert "## Known gap: python_version_support breaks" in text
    assert "x: BumpCheck said SAFE (rules: R7)" in text
    assert "NSL-Downloader#49" not in text


def test_report_known_gap_omitted_without_false_safe_category(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("a", "BROKE", category="api_break"),))
    records = [record("a", "BROKE", "REVIEW", category="api_break")]

    text = report.render_report(directory, records, split="dev")

    assert "## Known gap:" not in text
    assert "NSL-Downloader#49" not in text


def test_report_computes_baselines_from_current_labeled_results(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (
            label("major", "BROKE", category="api_break"),
            label("patch", "BROKE", category="api_break"),
        ),
    )
    write_candidates(
        directory,
        (
            candidate("major", "1.2.3", "2.0.0"),
            candidate("patch", "1.2.3", "1.2.4"),
        ),
    )
    records = [
        record(
            "major",
            "BROKE",
            "SAFE",
            category="api_break",
            verdicts={
                "bumpcheck": "SAFE",
                "always_safe": "REVIEW",
                "semver_rule": "SAFE",
            },
        ),
        record(
            "patch",
            "BROKE",
            "SAFE",
            category="api_break",
            verdicts={
                "bumpcheck": "SAFE",
                "always_safe": "REVIEW",
                "semver_rule": "REVIEW",
            },
        ),
    ]

    text = report.render_report(directory, records, split="dev")

    assert "'Review major bumps, plus minor bumps while the major version is zero.'" in text
    assert "versions unavailable for 0 of 2 cases (baselines counted SAFE)" in text
    assert "Stored baseline verdicts in results.jsonl are not used." in text
    row = (
        "| false-safe rate | 2/2 = 100.0% [34.2, 100.0] | 2/2 = 100.0% [34.2, 100.0] "
        "| 1/2 = 50.0% [9.5, 90.5] |"
    )
    assert row in text


@pytest.mark.parametrize(
    ("old", "new", "review_expected"),
    [
        ("1.2.3", "2.0.0", True),
        ("1.2.3", "1.3.0", False),
        ("1.0.0", "2.0.0a1", True),
        ("1.0.0a1", "1.0.0b1", False),
        ("1!1.0", "1!2.0", True),
        ("1!1.0", "2!1.0", False),
        ("latest", "2.0.0", False),
        (None, "2.0.0", False),
    ],
)
def test_report_semver_rule_edge_cases(
    tmp_path: Path, old: str | None, new: str | None, review_expected: bool
) -> None:
    directory = base_dir(tmp_path, (label("case", "BROKE", category="api_break"),))
    write_candidates(directory, (candidate("case", old, new),))
    records = [record("case", "BROKE", "SAFE", category="api_break")]

    text = report.render_report(directory, records, split="dev")

    safe_cell = "1/1 = 100.0% [20.7, 100.0]"
    semver_cell = "0/1 = 0.0% [0.0, 79.3]" if review_expected else safe_cell
    assert f"| false-safe rate | {safe_cell} | {safe_cell} | {semver_cell} |" in text


def test_report_semver_rule_zero_major_minor_bump_is_review(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("case", "BROKE", category="api_break"),))
    write_candidates(directory, (candidate("case", "0.1.0", "0.2.0"),))
    records = [record("case", "BROKE", "SAFE", category="api_break")]

    text = report.render_report(directory, records, split="dev")

    safe_cell = "1/1 = 100.0% [20.7, 100.0]"
    assert f"| false-safe rate | {safe_cell} | {safe_cell} | 0/1 = 0.0% [0.0, 79.3] |" in text


def test_report_baseline_versions_fall_back_to_result_diagnostics(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("case", "BROKE", category="api_break"),))
    write_candidates(directory)
    records = [
        record(
            "case",
            "BROKE",
            "SAFE",
            category="api_break",
            versions=("1.2.3", "2.0.0"),
            verdicts={
                "bumpcheck": "SAFE",
                "always_safe": "SAFE",
                "semver_rule": "SAFE",
            },
        )
    ]

    text = report.render_report(directory, records, split="dev")

    assert "versions unavailable for 0 of 1 cases (baselines counted SAFE)" in text
    safe_cell = "1/1 = 100.0% [20.7, 100.0]"
    assert f"| false-safe rate | {safe_cell} | {safe_cell} | 0/1 = 0.0% [0.0, 79.3] |" in text


def test_report_baselines_respect_split_selection(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (
            label("dev-case", "BROKE", category="api_break"),
            label("staging-case", "BROKE", category="api_break"),
        ),
    )
    write_candidates(
        directory,
        (
            candidate("dev-case", "1.2.3", "2.0.0", split="dev"),
            candidate("staging-case", "1.2.3", "1.2.4", split="staging"),
        ),
    )
    records = [
        record("dev-case", "BROKE", "SAFE", category="api_break", split="dev"),
        record("staging-case", "BROKE", "SAFE", category="api_break", split="staging"),
    ]

    dev_text = report.render_report(directory, records, split="dev")

    assert "scored cases in this split: 1." in dev_text
    assert "staging-case" not in dev_text
    safe_cell = "1/1 = 100.0% [20.7, 100.0]"
    assert f"| false-safe rate | {safe_cell} | {safe_cell} | 0/1 = 0.0% [0.0, 79.3] |" in dev_text

    staging_text = report.render_report(directory, records, split="staging")

    assert "dev-case" not in staging_text
    assert f"| false-safe rate | {safe_cell} | {safe_cell} | {safe_cell} |" in staging_text

    all_text = report.render_report(directory, records, split="all")

    assert "scored cases in this split: 2." in all_text
    all_row = (
        "| false-safe rate | 2/2 = 100.0% [34.2, 100.0] | 2/2 = 100.0% [34.2, 100.0] "
        "| 1/2 = 50.0% [9.5, 90.5] |"
    )
    assert all_row in all_text


def test_report_baselines_recompute_after_relabel(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (
            label("a", "BROKE", category="api_break"),
            label("b", "OK"),
        ),
    )
    write_candidates(
        directory,
        (
            candidate("a", "1.2.3", "2.0.0"),
            candidate("b", "1.2.3", "1.2.4"),
        ),
    )
    records = [
        record(
            "a",
            "BROKE",
            "SAFE",
            category="api_break",
            verdicts={
                "bumpcheck": "SAFE",
                "always_safe": "SAFE",
                "semver_rule": "SAFE",
            },
        ),
        record(
            "b",
            "OK",
            "SAFE",
            verdicts={
                "bumpcheck": "SAFE",
                "always_safe": "SAFE",
                "semver_rule": "REVIEW",
            },
        ),
    ]

    initial = report.render_report(directory, records, split="dev")

    safe_cell = "1/1 = 100.0% [20.7, 100.0]"
    detected_cell = "0/1 = 0.0% [0.0, 79.3]"
    assert f"| false-safe rate | {safe_cell} | {safe_cell} | {detected_cell} |" in initial

    (directory / "labels.jsonl").write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in (
                label("a", "OK"),
                label("b", "BROKE", category="api_break"),
            )
        ),
        encoding="utf-8",
    )
    relabeled = report.render_report(directory, records, split="dev")

    assert f"| false-safe rate | {safe_cell} | {safe_cell} | {safe_cell} |" in relabeled
    assert "b: BumpCheck said SAFE" in relabeled


def test_report_baseline_metrics_use_wilson_and_keep_small_n_warning(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("case", "BROKE", category="api_break"),))
    write_candidates(directory, (candidate("case", "1.2.3", "2.0.0"),))
    records = [
        record(
            "case",
            "BROKE",
            "SAFE",
            category="api_break",
            verdicts={
                "bumpcheck": "SAFE",
                "always_safe": "SAFE",
                "semver_rule": "SAFE",
            },
        )
    ]

    text = report.render_report(directory, records, split="dev")

    assert "> n is small; do not quote these numbers publicly" in text
    assert "Every rate is shown as raw k/n with a 95% Wilson interval." in text
    safe_cell = "1/1 = 100.0% [20.7, 100.0]"
    assert f"| false-safe rate | {safe_cell} | {safe_cell} | 0/1 = 0.0% [0.0, 79.3] |" in text


def test_report_counts_cases_with_unavailable_versions(tmp_path: Path) -> None:
    directory = base_dir(
        tmp_path,
        (
            label("good", "BROKE", category="api_break"),
            label("missing", "BROKE", category="api_break"),
            label("invalid", "BROKE", category="api_break"),
        ),
    )
    write_candidates(
        directory,
        (
            candidate("good", "1.2.3", "2.0.0"),
            candidate("missing", None, "2.0.0"),
            candidate("invalid", "latest", "2.0.0"),
        ),
    )
    records = [
        record("good", "BROKE", "SAFE", category="api_break"),
        record("missing", "BROKE", "SAFE", category="api_break"),
        record("invalid", "BROKE", "SAFE", category="api_break"),
    ]

    text = report.render_report(directory, records, split="dev")

    assert "versions unavailable for 2 of 3 cases (baselines counted SAFE)" in text
    row = (
        "| false-safe rate | 3/3 = 100.0% [43.8, 100.0] | 3/3 = 100.0% [43.8, 100.0] "
        "| 2/3 = 66.7% [20.8, 93.9] |"
    )
    assert row in text


def test_report_selection_bias_text_includes_sampled_by_mix(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("a", "BROKE", category="api_break"),))
    write_candidates(
        directory,
        (
            candidate("a", "1.2.3", "2.0.0", sampled_by="any"),
            candidate("b", "1.2.3", "2.0.0", sampled_by="any"),
            candidate("c", "1.2.3", "2.0.0", sampled_by="failure"),
            candidate("d", "1.2.3", "2.0.0", sampled_by="success"),
            candidate("e", "1.2.3", "2.0.0", sampled_by="success"),
            candidate("f", "1.2.3", "2.0.0", sampled_by="success"),
            candidate("g", "1.2.3", "2.0.0", split="staging", sampled_by="manual"),
        ),
    )
    records = [record("a", "BROKE", "SAFE", category="api_break")]

    text = report.render_report(directory, records, split="dev")

    assert "Loaded candidates: 7; scored cases in this split: 1." in text
    assert "Sampled-by mix: any=2, failure=1, success=3, manual=1" in text
    assert "Failure-sampled candidates inflate the BROKE share" in text
    assert "~2% BROKE" not in text


def test_report_baseline_table_has_abstention_column(tmp_path: Path) -> None:
    directory = base_dir(tmp_path, (label("case", "BROKE", category="api_break"),))
    write_candidates(directory, (candidate("case", "1.2.3", "2.0.0"),))
    records = [record("case", "BROKE", "UNKNOWN", category="api_break")]

    text = report.render_report(directory, records, split="dev")

    assert "| metric | bumpcheck | always_safe | semver_rule |" in text
    assert (
        "| abstention rate (UNKNOWN) | 1/1 = 100.0% [20.7, 100.0] "
        "| 0/1 = 0.0% [0.0, 79.3] | 0/1 = 0.0% [0.0, 79.3] |"
    ) in text
    assert "## Abstention rate by label" in text
    assert (
        "| BROKE | 1/1 = 100.0% [20.7, 100.0] | 0/1 = 0.0% [0.0, 79.3] | 0/1 = 0.0% [0.0, 79.3] |"
    ) in text
