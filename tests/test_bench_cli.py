from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bumpcheck import cli
from bumpcheck.bench.labels import load_labels
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


def _label(case_id: str, label: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "case_id": case_id,
        "round": 1,
        "label": label,
        "labeled_on": "2026-01-01",
    }
    payload.update(overrides)
    return payload


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


def _patch_snapshot_status(monkeypatch, *, status: str, error: str | None = None) -> None:
    payload: dict[str, object] = {"case_id": "owner/repo#1", "status": status}
    if error is not None:
        payload["error"] = error
    monkeypatch.setattr(cli, "snapshot_case", lambda *args, **kwargs: payload)


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


def test_bench_label_unknown_case_exits_two(tmp_path: Path) -> None:
    directory = _base(tmp_path)

    result = runner.invoke(
        app,
        ["bench", "label", "--dir", str(directory), "--case", "owner/repo#999"],
    )

    assert result.exit_code == 2
    assert "candidate not found: owner/repo#999" in result.output


def test_bench_label_case_full_id_broke_persists(tmp_path: Path) -> None:
    directory = _base(tmp_path)

    result = runner.invoke(
        app,
        ["bench", "label", "--dir", str(directory), "--case", "owner/repo#1"],
        input="b\napi_break\nlog_names_package\nImportError: boom\n\nnotes\n",
    )

    assert result.exit_code == 0, result.output
    stored = load_labels(directory / "labels.jsonl")
    label = stored[("owner/repo#1", 1)]
    assert label.label == "BROKE"
    assert label.break_category == "api_break"
    assert label.evidence_kind == "log_names_package"
    assert label.evidence == "ImportError: boom"
    assert label.labeled_on == date.today()


def test_bench_label_case_full_id_exclude_persists(tmp_path: Path) -> None:
    directory = _base(tmp_path)

    result = runner.invoke(
        app,
        ["bench", "label", "--dir", str(directory), "--case", "owner/repo#1"],
        input="x\nsuperseded\nclosed by later PR\n\n",
    )

    assert result.exit_code == 0, result.output
    label = load_labels(directory / "labels.jsonl")[("owner/repo#1", 1)]
    assert label.label == "EXCLUDE"
    assert label.exclude_reason == "superseded"
    assert label.evidence == "closed by later PR"


def test_bench_label_case_shorthand_stores_full_id(tmp_path: Path) -> None:
    directory = _base(tmp_path)

    result = runner.invoke(
        app,
        ["bench", "label", "--dir", str(directory), "--case", "repo#1"],
        input="x\npreexisting_failure\nalso failed on base\n\n",
    )

    assert result.exit_code == 0, result.output
    assert "saved EXCLUDE for owner/repo#1" in result.output
    stored = load_labels(directory / "labels.jsonl")
    assert stored[("owner/repo#1", 1)].label == "EXCLUDE"


def test_bench_label_ambiguous_shorthand_exits_two_and_keeps_store(tmp_path: Path) -> None:
    candidates = []
    for case_id, repo in (("a/widgets#1", "a/widgets"), ("b/widgets#1", "b/widgets")):
        candidate = _candidate()
        candidate["case_id"] = case_id
        candidate["repo"] = repo
        candidates.append(candidate)
    (tmp_path / "candidates.jsonl").write_text(
        "".join(json.dumps(candidate) + "\n" for candidate in candidates), encoding="utf-8"
    )
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text(json.dumps(_label("z/other#9", "OK")) + "\n", encoding="utf-8")
    before = labels_path.read_bytes()

    result = runner.invoke(
        app,
        ["bench", "label", "--dir", str(tmp_path), "--case", "widgets#1"],
        input="q\n",
    )

    assert result.exit_code == 2
    assert "ambiguous case id 'widgets#1'" in result.output
    assert "a/widgets#1" in result.output and "b/widgets#1" in result.output
    assert labels_path.read_bytes() == before


def test_bench_label_relabel_full_id_replaces_existing_label(tmp_path: Path) -> None:
    old = _label(
        "owner/repo#1",
        "EXCLUDE",
        exclude_reason="superseded",
        evidence="wrong case",
        notes="old note",
    )
    directory = _base(tmp_path, labels=[old])

    result = runner.invoke(
        app,
        ["bench", "label", "--dir", str(directory), "--case", "owner/repo#1", "--relabel"],
        input="x\npreexisting_failure\ncorrect evidence\n\n",
    )

    assert result.exit_code == 0, result.output
    stored = load_labels(directory / "labels.jsonl")
    label = stored[("owner/repo#1", 1)]
    assert label.exclude_reason == "preexisting_failure"
    assert label.evidence == "correct evidence"
    assert label.labeled_on == date.today()
    assert "replaced previous label: EXCLUDE exclude_reason=superseded" in result.output
    history = json.loads(
        (directory / "labels.history.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert history["previous"]["evidence"] == "wrong case"


def test_bench_label_relabel_shorthand_replaces_existing_label(tmp_path: Path) -> None:
    old = _label("owner/repo#1", "EXCLUDE", exclude_reason="superseded", evidence="wrong case")
    directory = _base(tmp_path, labels=[old])

    result = runner.invoke(
        app,
        ["bench", "label", "--dir", str(directory), "--case", "repo#1", "--relabel"],
        input="x\npreexisting_failure\ncorrect evidence\n\n",
    )

    assert result.exit_code == 0, result.output
    assert "saved EXCLUDE for owner/repo#1" in result.output
    label = load_labels(directory / "labels.jsonl")[("owner/repo#1", 1)]
    assert label.exclude_reason == "preexisting_failure"
    history = json.loads(
        (directory / "labels.history.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert history["previous"]["exclude_reason"] == "superseded"


def test_bench_label_relabel_without_case_exits_two(tmp_path: Path) -> None:
    directory = _base(tmp_path)
    labels_path = directory / "labels.jsonl"
    before = labels_path.read_bytes()

    result = runner.invoke(
        app,
        ["bench", "label", "--dir", str(directory), "--relabel"],
        input="q\n",
    )

    assert result.exit_code == 2
    assert "--relabel requires at least one --case" in result.output
    assert labels_path.read_bytes() == before
    assert not (directory / "labels.history.jsonl").exists()


def test_bench_label_relabel_rejects_incompatible_options(tmp_path: Path) -> None:
    directory = _base(tmp_path, labels=[_label("owner/repo#1", "OK")])
    labels_path = directory / "labels.jsonl"
    before = labels_path.read_bytes()
    incompatible = [
        ["--sample", "1.0"],
        ["--only", "OK_SUSPECT"],
        ["--also-dir", str(directory)],
    ]

    for extra in incompatible:
        result = runner.invoke(
            app,
            [
                "bench",
                "label",
                "--dir",
                str(directory),
                "--case",
                "owner/repo#1",
                "--relabel",
                *extra,
            ],
            input="q\n",
        )
        assert result.exit_code == 2
        assert "--relabel cannot be combined" in result.output

    assert labels_path.read_bytes() == before
    assert not (directory / "labels.history.jsonl").exists()


def test_bench_label_already_labeled_drop_message_has_relabel_hint(tmp_path: Path) -> None:
    directory = _base(tmp_path, labels=[_label("owner/repo#1", "OK")])

    result = runner.invoke(
        app,
        ["bench", "label", "--dir", str(directory), "--case", "owner/repo#1"],
        input="q\n",
    )

    assert result.exit_code == 0, result.output
    assert (
        "owner/repo#1 (already labeled this round; use --relabel with --case to replace it)"
        in result.output
    )


def test_bench_label_stats_prints_total_line(tmp_path: Path) -> None:
    directory = _base(
        tmp_path,
        labels=[
            _label("owner/repo#1", "OK"),
            {
                "case_id": "a/b#2",
                "round": 1,
                "label": "EXCLUDE",
                "exclude_reason": "superseded",
                "evidence": "closed",
                "labeled_on": "2026-01-01",
            },
            {"case_id": "c/d#3", "round": 2, "label": "OK", "labeled_on": "2026-01-01"},
        ],
    )

    result = runner.invoke(app, ["bench", "label-stats", "--dir", str(directory)])

    assert result.exit_code == 0, result.output
    lines = result.output.replace("\r\n", "\n").splitlines()
    assert lines[0] == "labeled cases: 3 (round 1: 2, round 2: 1)"
    assert "round 1: BROKE=0 OK=1 EXCLUDE=1" in lines
    assert "round 2: BROKE=0 OK=1 EXCLUDE=0" in lines


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


def test_bench_snapshot_exits_one_when_case_status_failed(tmp_path: Path, monkeypatch) -> None:
    directory = _base(tmp_path)
    _patch_snapshot_status(monkeypatch, status="failed")

    result = runner.invoke(
        app,
        ["bench", "snapshot", "--dir", str(directory), "--offline", "--allow-unauthenticated"],
    )

    assert result.exit_code == 1, result.output


def test_bench_snapshot_prints_per_case_status_size_and_error(tmp_path: Path, monkeypatch) -> None:
    directory = _base(tmp_path)
    _patch_snapshot_status(monkeypatch, status="failed", error="boom")

    result = runner.invoke(
        app,
        ["bench", "snapshot", "--dir", str(directory), "--offline", "--allow-unauthenticated"],
    )
    output = result.output.replace("\r\n", "\n")

    assert "owner/repo#1: failed (" in output
    assert " bytes" in output
    assert "boom" in output


def test_bench_snapshot_prints_disk_usage_summary_before_failure_exit(
    tmp_path: Path, monkeypatch
) -> None:
    directory = _base(tmp_path)
    _patch_snapshot_status(monkeypatch, status="failed", error="boom")

    result = runner.invoke(
        app,
        ["bench", "snapshot", "--dir", str(directory), "--offline", "--allow-unauthenticated"],
    )
    output = result.output.replace("\r\n", "\n")

    assert result.exit_code == 1
    assert output.index("owner/repo#1: failed") < output.index("snapshot disk usage:")


def test_bench_snapshot_terminal_nonfailed_statuses_exit_zero(tmp_path: Path, monkeypatch) -> None:
    directory = _base(tmp_path)
    _patch_snapshot_status(monkeypatch, status="snapshot_incomplete")

    result = runner.invoke(
        app,
        ["bench", "snapshot", "--dir", str(directory), "--offline", "--allow-unauthenticated"],
    )

    assert result.exit_code == 0, result.output
    assert "owner/repo#1: snapshot_incomplete" in result.output


def test_bench_snapshot_redacts_failure_reason(tmp_path: Path, monkeypatch) -> None:
    directory = _base(tmp_path)
    reason = "https://user:secret@example.com/x?token=abc"
    _patch_snapshot_status(monkeypatch, status="failed", error=reason)

    result = runner.invoke(
        app,
        ["bench", "snapshot", "--dir", str(directory), "--offline", "--allow-unauthenticated"],
    )

    assert result.exit_code == 1
    assert "secret" not in result.output
    assert "token=abc" not in result.output
    assert "***@" in result.output
