from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bumpcheck.bench import eval as bench_eval
from bumpcheck.bench import eval_worker
from bumpcheck.bench.labels import Label
from bumpcheck.verdict import BumpVerdict, CheckResult


def write_case(
    tmp_path: Path,
    *,
    split: str = "dev",
    rounds: tuple[str, ...] = ("BROKE",),
    case_id: str = "owner/repo#1",
) -> dict:
    candidate = {
        "case_id": case_id,
        "repo": "owner/repo",
        "pr_number": 1,
        "package": "pkg",
        "old_version": "1.0.0",
        "new_version": "2.0.0",
        "base_sha": "base",
        "head_sha": "head",
        "lockfile_kind": "requirements",
        "lockfile_paths": ["requirements.txt"],
        "split": split,
        "sampled_by": "failure",
    }
    (tmp_path / "candidates.jsonl").write_text(json.dumps(candidate) + "\n", encoding="utf-8")
    lines = []
    for number, value in enumerate(rounds, start=1):
        label = Label(
            case_id=candidate["case_id"],
            round=number,
            label=value,
            break_category="api_break" if value == "BROKE" else None,
            evidence_kind="log_names_package" if value == "BROKE" else None,
            evidence="log names package" if value == "BROKE" else "",
            labeled_on=date(2026, 1, number),
        )
        lines.append(label.model_dump_json())
    (tmp_path / "labels.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return candidate


def result_payload(verdict: str = "SAFE") -> dict[str, Any]:
    return {
        "verdicts": {"bumpcheck": verdict, "always_safe": "SAFE", "semver_rule": "REVIEW"},
        "fired_rule_ids": {"bumpcheck": ["R2"]},
        "evidence_refs": [],
        "parse_failures": 0,
        "runtime_s": 0.1,
        "error_kind": None,
        "snapshot_status": "complete",
    }


def test_eval_defaults_to_dev_and_logs_test_runs(tmp_path: Path) -> None:
    write_case(tmp_path, split="dev")
    other = tmp_path / "other"
    other.mkdir()
    write_case(other, split="test", case_id="owner/repo#2")
    (tmp_path / "candidates.jsonl").write_text(
        (tmp_path / "candidates.jsonl").read_text(encoding="utf-8")
        + (other / "candidates.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "labels.jsonl").write_text(
        (tmp_path / "labels.jsonl").read_text(encoding="utf-8")
        + (other / "labels.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    fingerprint = {"id": "fp1", "git_sha": "abc", "dirty": False, "source_sha256": "x"}
    calls: list[str] = []

    def counting_runner(payload: dict, timeout: float) -> dict:
        calls.append(payload["case_id"])
        return result_payload()

    def simple_runner(payload: dict, timeout: float) -> dict:
        return result_payload()

    dev = bench_eval.evaluate(
        tmp_path, split="dev", fingerprint=fingerprint, runner=counting_runner
    )
    assert len(dev) == 1
    assert dev[0]["split"] == "dev"
    assert len(calls) == 1

    test = bench_eval.evaluate(
        tmp_path, split="test", fingerprint=fingerprint, runner=simple_runner
    )
    assert len(test) == 2
    log = (tmp_path / "eval" / "test_runs.log").read_text(encoding="utf-8")
    assert "split=test" in log


def test_resume_skips_only_matching_fingerprints(tmp_path: Path) -> None:
    write_case(tmp_path)
    old = {"id": "old", "git_sha": "a", "dirty": False, "source_sha256": "x"}
    new = {"id": "new", "git_sha": "b", "dirty": False, "source_sha256": "y"}
    calls: list[str] = []

    def runner(value: str):
        def run(payload: dict, timeout: float) -> dict:
            calls.append(value)
            return result_payload()

        return run

    bench_eval.evaluate(tmp_path, fingerprint=old, runner=runner("owner/repo#1"))
    assert calls == ["owner/repo#1"]

    bench_eval.evaluate(tmp_path, fingerprint=old, runner=runner("again"))
    assert calls == ["owner/repo#1"]

    records = bench_eval.evaluate(
        tmp_path,
        fingerprint=new,
        runner=runner("mixed"),
    )
    assert calls[-1] == "mixed"
    assert records[0]["fingerprint"]["id"] == "new"


def test_disputed_labels_are_excluded_and_recorded(tmp_path: Path) -> None:
    write_case(tmp_path, rounds=("BROKE", "OK"))

    def forbidden(payload: dict, timeout: float) -> dict:
        raise AssertionError("disputed cases must not run")

    records = bench_eval.evaluate(tmp_path, fingerprint={"id": "fp"}, runner=forbidden)

    assert records[0]["label"] == "DISPUTED"
    assert records[0]["error_kind"] == "disputed_label"
    assert records[0]["verdicts"]["bumpcheck"] == "UNKNOWN"


def test_timeout_returns_unknown(tmp_path: Path, monkeypatch) -> None:
    class FakeProcess:
        pid = 42
        returncode = None

        def poll(self) -> None:
            return None

        def communicate(self, payload: str, timeout: float):
            raise subprocess.TimeoutExpired("worker", timeout)

    monkeypatch.setattr(bench_eval.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(bench_eval, "_kill_process", lambda process: None)

    result = bench_eval.run_worker({"case_id": "owner/repo#1"}, 0.1)

    assert result == {"error_kind": "timeout", "verdicts": {}, "parse_failures": 0}


def test_worker_records_unparsable_file_count(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "snapshot"
    (root / "old").mkdir(parents=True)
    (root / "new").mkdir()
    (root / "repo").mkdir()
    (root / "old" / "meta.json").write_bytes(b"{}")
    (root / "new" / "meta.json").write_bytes(b"{}")
    (root / "old" / "old.whl").write_bytes(b"old")
    (root / "new" / "new.whl").write_bytes(b"new")
    (root / "old-lock.txt").write_text("pkg==1\n", encoding="utf-8")
    (root / "new-lock.txt").write_text("pkg==2\n", encoding="utf-8")
    manifest = {
        "status": "complete",
        "old": {
            "metadata_url": "https://pypi.org/pypi/pkg/1.0.0/json",
            "metadata_file": "old/meta.json",
            "wheel_url": "https://files/pythonhosted/old.whl",
            "wheel_file": "old/old.whl",
        },
        "new": {
            "metadata_url": "https://pypi.org/pypi/pkg/2.0.0/json",
            "metadata_file": "new/meta.json",
            "wheel_url": "https://files/pythonhosted/new.whl",
            "wheel_file": "new/new.whl",
        },
        "lockfiles": {"requirements.txt": {"old": "old-lock.txt", "new": "new-lock.txt"}},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    verdict = BumpVerdict(
        package="pkg",
        old_version="1.0.0",
        new_version="2.0.0",
        verdict="SAFE",
        rule_id="R1",
        rationale="ok",
    )

    def fake_check(*args: object, **kwargs: object) -> CheckResult:
        usage_fn = kwargs["usage_fn"]
        usage_fn(root / "repo", "pkg", ["foo"])
        return CheckResult(overall="SAFE", bumps=(verdict,))

    monkeypatch.setattr(eval_worker, "run_check", fake_check)
    monkeypatch.setattr(
        eval_worker,
        "trace_usage",
        lambda *args, **kwargs: SimpleNamespace(
            files_skipped=[SimpleNamespace(reason="unparsable")]
        ),
    )
    payload = {
        "snapshot": str(root),
        "candidate": {
            "package": "pkg",
            "old_version": "1.0.0",
            "new_version": "2.0.0",
            "lockfile_kind": "requirements",
            "lockfile_paths": ["requirements.txt"],
        },
    }

    result = eval_worker.run_case(payload)

    assert result["parse_failures"] == 1
    assert result["verdicts"]["bumpcheck"] == "SAFE"
