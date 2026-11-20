from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bumpcheck.bench.snapshot import case_slug
from bumpcheck.cli import app

runner = CliRunner()


def _candidate() -> dict[str, object]:
    return {
        "case_id": "owner/repo#1",
        "repo": "owner/repo",
        "pr_number": 1,
        "package": "pkg",
        "old_version": "1.0.0",
        "new_version": "2.0.0",
        "base_sha": "base",
        "head_sha": "head",
        "lockfile_kind": "requirements",
        "lockfile_paths": ["requirements.txt"],
        "split": "dev",
        "sampled_by": "failure",
    }


def _label(case_id: str, label: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "round": 1,
        "label": label,
        "labeled_on": "2026-01-01",
    }


def _base(tmp_path: Path, *, labels: list[dict[str, object]] | None = None) -> Path:
    candidate = _candidate()
    (tmp_path / "candidates.jsonl").write_text(json.dumps(candidate) + "\n", encoding="utf-8")
    payload = "".join(json.dumps(label) + "\n" for label in labels or ())
    (tmp_path / "labels.jsonl").write_text(payload, encoding="utf-8")
    case_dir = tmp_path / "snapshots" / case_slug(candidate)
    case_dir.mkdir(parents=True)
    (case_dir / "manifest.json").write_text(
        json.dumps({"case_id": candidate["case_id"], "status": "complete"}) + "\n",
        encoding="utf-8",
    )
    (case_dir / "payload.bin").write_bytes(b"abc")
    return tmp_path


def test_bench_snapshot_prints_disk_usage(tmp_path: Path) -> None:
    directory = _base(tmp_path)

    result = runner.invoke(
        app,
        ["bench", "snapshot", "--dir", str(directory), "--offline", "--allow-unauthenticated"],
    )

    assert result.exit_code == 0, result.output
    output = result.output.replace("\r\n", "\n")
    assert "owner/repo#1: complete" in output
    usage = int(output.split("snapshot disk usage: ")[1].split()[0])
    assert usage >= 3


def test_bench_report_writes_dev_report_by_default(tmp_path: Path) -> None:
    directory = _base(tmp_path, labels=[_label("owner/repo#1", "OK")])
    eval_dir = directory / "eval"
    eval_dir.mkdir()
    record = {
        "case_id": "owner/repo#1",
        "label": "OK",
        "break_category": None,
        "split": "dev",
        "sampled_by": "failure",
        "fingerprint": {"id": "fp", "git_sha": "abc", "dirty": False, "source_sha256": "x"},
        "verdicts": {"bumpcheck": "SAFE", "always_safe": "SAFE", "semver_rule": "SAFE"},
        "fired_rule_ids": {"bumpcheck": []},
        "error_kind": None,
    }
    (eval_dir / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = runner.invoke(app, ["bench", "report", "--dir", str(directory)])

    assert result.exit_code == 0, result.output
    assert "# Benchmark report (split: dev)" in result.output
    assert (eval_dir / "report.md").is_file()


@pytest.mark.parametrize("write_empty", [False, True])
def test_bench_eval_with_no_labels_prints_message(tmp_path: Path, write_empty: bool) -> None:
    directory = tmp_path / "bench"
    directory.mkdir()
    labels_path = directory / "labels.jsonl"
    if write_empty:
        labels_path.write_text("\n", encoding="utf-8")

    result = runner.invoke(app, ["bench", "eval", "--dir", str(directory), "--split", "all"])

    expected = f"0 labeled cases found in {labels_path} (or file missing)"
    assert result.exit_code == 0, result.output
    assert result.output.replace("\r\n", "\n").strip() == expected
    assert not (directory / "eval").exists()


@pytest.mark.parametrize("write_empty", [False, True])
def test_bench_report_with_no_labels_prints_message(tmp_path: Path, write_empty: bool) -> None:
    directory = tmp_path / "bench"
    directory.mkdir()
    labels_path = directory / "labels.jsonl"
    if write_empty:
        labels_path.write_text("\n", encoding="utf-8")

    result = runner.invoke(app, ["bench", "report", "--dir", str(directory), "--split", "all"])

    expected = f"0 labeled cases found in {labels_path} (or file missing)"
    assert result.exit_code == 0, result.output
    assert result.output.replace("\r\n", "\n").strip() == expected
    assert not (directory / "eval" / "report.md").exists()
