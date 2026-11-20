from __future__ import annotations

from pathlib import Path
from typing import Any

from bumpcheck.bench import report


def base_dir(tmp_path: Path) -> Path:
    (tmp_path / "candidates.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "labels.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "excluded.jsonl").write_text(
        '{"case_id":"x","repo":"o/r","pr_number":2,"reason":"parse_mismatch"}\n',
        encoding="utf-8",
    )
    return tmp_path


def fingerprint(value: str) -> dict[str, Any]:
    return {"id": value, "git_sha": value, "dirty": False, "source_sha256": value}


def record(
    case_id: str,
    label: str,
    verdict: str,
    *,
    category: str | None = None,
    rules: list[str] | None = None,
    error_kind: str | None = None,
    fp: str = "one",
    split: str = "dev",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "label": label,
        "break_category": category,
        "split": split,
        "sampled_by": "failure",
        "fingerprint": fingerprint(fp),
        "verdicts": {"bumpcheck": verdict, "always_safe": "SAFE", "semver_rule": "REVIEW"},
        "fired_rule_ids": {"bumpcheck": rules or []},
        "evidence_refs": [],
        "error_kind": error_kind,
        "parse_failures": 0,
        "runtime_s": 0.1,
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
    directory = base_dir(tmp_path)
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
    known_gap = text.index("## Known gap: Python-version-support breaks")
    assert known_gap < text.index("## Headline metrics")
    assert "o/r#1" not in text
    assert "owner/repo" not in text
    assert "b: BumpCheck said UNKNOWN" in text or ": BumpCheck said UNKNOWN" in text
    assert "0/2 = 0.0% [0.0, 65.8]" in text
    assert "1/2 = 50.0% [9.5, 90.5]" in text
    assert "1/4 = 25.0% [4.6, 69.9]" in text


def test_report_lists_disputed_and_mixed_fingerprints(tmp_path: Path) -> None:
    directory = base_dir(tmp_path)
    records = [
        record("a", "BROKE", "SAFE"),
        record("b", "DISPUTED", "UNKNOWN", fp="two"),
    ]

    text = report.render_report(directory, records, split="dev")

    assert "Warning: mixed fingerprints: one, two" in text
    assert "Disputed cases excluded from metrics: b" in text
    assert "round 1 unless round 2 exists." in text
    assert "parse_mismatch=1" in text
    assert "~2% BROKE" in text


def test_report_rule_tp_fp_uses_break_category_of_label(tmp_path: Path) -> None:
    directory = base_dir(tmp_path)
    records = [
        record("a", "BROKE", "REVIEW", category="api_break", rules=["R7"]),
        record("b", "OK", "SAFE", rules=["R7"]),
    ]

    text = report.render_report(directory, records, split="dev")

    assert "| R7 | 2 | 1 | 1 |" in text


def test_write_report_writes_markdown(tmp_path: Path) -> None:
    directory = base_dir(tmp_path)
    records = [record("a", "OK", "SAFE")]

    text = report.write_report(directory, records, split="dev")

    assert text == (directory / "eval" / "report.md").read_text(encoding="utf-8")
