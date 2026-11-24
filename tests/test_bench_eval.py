from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bumpcheck.apidiff import ApiChange, ApiDiffResult
from bumpcheck.bench import eval as bench_eval
from bumpcheck.bench import eval_worker, report
from bumpcheck.bench.labels import Label
from bumpcheck.models import Bump
from bumpcheck.rules import decide
from bumpcheck.usage import SkippedFile, UsageResult
from bumpcheck.verdict import BumpVerdict, CheckResult, Evidence


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
            evidence=(
                "log names package"
                if value == "BROKE"
                else "excluded for test"
                if value == "EXCLUDE"
                else ""
            ),
            exclude_reason="not_attributable" if value == "EXCLUDE" else None,
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


def write_label(
    tmp_path: Path,
    case_id: str,
    value: str,
    *,
    category: str | None = None,
    round_number: int = 1,
) -> None:
    label = Label(
        case_id=case_id,
        round=round_number,
        label=value,
        break_category=category,
        evidence_kind="log_names_package" if value == "BROKE" else None,
        evidence=(
            "log names package"
            if value == "BROKE"
            else "excluded for test"
            if value == "EXCLUDE"
            else ""
        ),
        exclude_reason="not_attributable" if value == "EXCLUDE" else None,
        labeled_on=date(2026, 1, round_number),
    )
    (tmp_path / "labels.jsonl").write_text(label.model_dump_json() + "\n", encoding="utf-8")


def snapshot_root(
    tmp_path: Path,
    *,
    status: str = "complete",
    requires_python: str | None = None,
    blank_requires_python: bool = False,
    include_metadata_file: bool = True,
) -> Path:
    root = tmp_path / "snapshot"
    (root / "old").mkdir(parents=True, exist_ok=True)
    (root / "new").mkdir(parents=True, exist_ok=True)
    (root / "repo").mkdir(parents=True, exist_ok=True)
    (root / "old-lock.txt").write_text("pkg==1\n", encoding="utf-8")
    (root / "new-lock.txt").write_text("pkg==2\n", encoding="utf-8")
    (root / "old" / "old.whl").write_bytes(b"old")
    (root / "new" / "new.whl").write_bytes(b"new")
    if include_metadata_file:
        payload = {"info": {"requires_python": requires_python or ""}}
        for side in ("old", "new"):
            (root / side / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    manifest: dict[str, Any] = {
        "status": status,
        "old": {
            "wheel_url": "https://files.pythonhosted/old.whl",
            "wheel_file": "old/old.whl",
        },
        "new": {
            "wheel_url": "https://files.pythonhosted/new.whl",
            "wheel_file": "new/new.whl",
        },
        "lockfiles": {"requirements.txt": {"old": "old-lock.txt", "new": "new-lock.txt"}},
    }
    if include_metadata_file:
        manifest["old"]["metadata_file"] = "old/metadata.json"
        manifest["new"]["metadata_file"] = "new/metadata.json"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def worker_payload(root: Path, *, package: str = "pkg") -> dict[str, Any]:
    return {
        "snapshot": str(root),
        "candidate": {
            "package": package,
            "old_version": "1.0.0",
            "new_version": "2.0.0",
            "lockfile_kind": "requirements",
            "lockfile_paths": ["requirements.txt"],
        },
    }


def make_verdict(
    *,
    evidence: tuple[Evidence, ...] = (),
    rule_id: str = "R7",
    verdict: str = "SAFE",
) -> BumpVerdict:
    return BumpVerdict(
        package="pkg",
        old_version="1.0.0",
        new_version="2.0.0",
        verdict=verdict,
        rule_id=rule_id,
        rationale="test verdict",
        evidence=evidence,
        api_status="ok",
    )


def make_api(changes: list[ApiChange]) -> ApiDiffResult:
    return ApiDiffResult(
        package="pkg",
        old_version="1.0.0",
        new_version="2.0.0",
        status="ok",
        changes=changes,
        top_level=["pkg"],
    )


def fake_check(
    verdicts: tuple[BumpVerdict, ...],
    *,
    apis: dict[int, ApiDiffResult] | None = None,
    usages: dict[int, UsageResult] | None = None,
    overall: str = "SAFE",
):
    apis = apis or {}
    usages = usages or {}

    def run(*args: object, **kwargs: object) -> CheckResult:
        hook = kwargs["on_bump"]
        for verdict in verdicts:
            hook(verdict, apis.get(id(verdict)), usages.get(id(verdict)))
        return CheckResult(overall=overall, bumps=verdicts)

    return run


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


def test_relabel_broke_to_ok_reuses_verdict_and_report_shows_new_label(tmp_path: Path) -> None:
    write_case(tmp_path)
    fingerprint = {"id": "fp1"}
    calls: list[str] = []

    def runner(payload: dict, timeout: float) -> dict:
        calls.append(payload["case_id"])
        return result_payload()

    records = bench_eval.evaluate(tmp_path, fingerprint=fingerprint, runner=runner)
    assert records[0]["label"] == "BROKE"
    assert calls == ["owner/repo#1"]

    write_label(tmp_path, "owner/repo#1", "OK")
    records = bench_eval.evaluate(tmp_path, fingerprint=fingerprint, runner=runner)
    assert calls == ["owner/repo#1"]
    assert records[0]["label"] == "BROKE"

    text = report.render_report(tmp_path, records, split="dev")
    assert "| OK | 0/1 = 0.0%" in text, text
    assert "Ignored result rows (unlabeled or label/category changed): 1" in text


def test_relabel_exclude_to_broke_case_gets_evaluated(tmp_path: Path) -> None:
    write_case(tmp_path, rounds=("EXCLUDE",))
    calls: list[str] = []

    def runner(payload: dict, timeout: float) -> dict:
        calls.append(payload["case_id"])
        return result_payload()

    bench_eval.evaluate(tmp_path, fingerprint={"id": "fp1"}, runner=runner)
    assert calls == []

    write_label(tmp_path, "owner/repo#1", "BROKE", category="api_break")
    records = bench_eval.evaluate(tmp_path, fingerprint={"id": "fp1"}, runner=runner)

    assert calls == ["owner/repo#1"]
    assert records[0]["label"] == "BROKE"


def test_exclude_labels_are_skipped_by_eval(tmp_path: Path) -> None:
    write_case(tmp_path, rounds=("EXCLUDE",))

    def forbidden(payload: dict, timeout: float) -> dict:
        raise AssertionError("excluded cases must not run")

    records = bench_eval.evaluate(tmp_path, fingerprint={"id": "fp1"}, runner=forbidden)

    assert records == []


def test_eval_recomputes_records_with_stale_results_schema_version(tmp_path: Path) -> None:
    write_case(tmp_path)
    fingerprint = {"id": "fp1"}
    stale = {
        "case_id": "owner/repo#1",
        "label": "BROKE",
        "break_category": "api_break",
        "split": "dev",
        "fingerprint": fingerprint,
        "results_schema_version": 1,
        **result_payload("SAFE"),
    }
    bench_eval.write_results(tmp_path / "eval" / "results.jsonl", [stale])
    calls: list[str] = []

    def runner(payload: dict, timeout: float) -> dict:
        calls.append(payload["case_id"])
        return result_payload("UNKNOWN")

    records = bench_eval.evaluate(tmp_path, fingerprint=fingerprint, runner=runner)

    assert calls == ["owner/repo#1"]
    assert records[0]["verdicts"]["bumpcheck"] == "UNKNOWN"
    assert records[0]["results_schema_version"] == bench_eval.RESULTS_SCHEMA_VERSION


def test_eval_stores_verdict_and_reason_correctness(tmp_path: Path) -> None:
    write_case(tmp_path)
    fingerprint = {"id": "fp1"}

    def runner(payload: dict, timeout: float) -> dict:
        payload_result = result_payload("LIKELY_BREAKING")
        payload_result["fired_rule_ids"] = {"bumpcheck": ["R4"]}
        return payload_result

    records = bench_eval.evaluate(tmp_path, fingerprint=fingerprint, runner=runner)

    assert records[0]["verdict_correct"] is True
    assert records[0]["reason_correct"] is True
    text = report.render_report(tmp_path, records, split="dev")
    assert "| reason-correct recall (BROKE) | 1/1" in text


def test_correctness_flags_use_break_category_mapping() -> None:
    dependency = {
        "label": "BROKE",
        "break_category": "dependency_conflict",
        "verdicts": {"bumpcheck": "LIKELY_BREAKING"},
        "fired_rule_ids": {"bumpcheck": ["R10"]},
    }
    wrong_reason = {**dependency, "fired_rule_ids": {"bumpcheck": ["R5"]}}
    ok = {
        "label": "OK",
        "verdicts": {"bumpcheck": "SAFE"},
        "fired_rule_ids": {"bumpcheck": ["R7"]},
    }

    assert bench_eval.correctness_flags(dependency) == (True, True)
    assert bench_eval.correctness_flags(wrong_reason) == (True, False)
    assert bench_eval.correctness_flags(ok) == (True, True)


def test_worker_persists_per_bump_diagnostics(tmp_path: Path, monkeypatch) -> None:
    root = snapshot_root(tmp_path)
    change = ApiChange(
        kind="PARAMETER_REMOVED",
        symbol="pkg.Client",
        severity="high",
        detail="removed timeout",
        parameter="timeout",
    )
    api = make_api([change])
    usage = UsageResult(
        package="pkg",
        symbols=["pkg.Client"],
        files_scanned=2,
        files_skipped=[SkippedFile(file="broken.py", reason="unparsable")],
        risks=["dynamic_import_unknown_target"],
    )
    verdict = make_verdict()
    monkeypatch.setattr(
        eval_worker,
        "run_check",
        fake_check((verdict,), apis={id(verdict): api}, usages={id(verdict): usage}),
    )

    result = eval_worker.run_case(worker_payload(root))

    diagnostics = result["diagnostics"]
    assert result["error_kind"] is None, result.get("worker_error")
    assert diagnostics["snapshot"] == {"status": "complete", "repo_present": True}
    assert len(diagnostics["bumps"]) == 1
    bump = diagnostics["bumps"][0]
    assert bump["package"] == "pkg"
    assert bump["rule_id"] == "R7"
    assert bump["api_diff"] == {
        "status": "ok",
        "reason": None,
        "change_count": 1,
        "changes_by_kind": {"PARAMETER_REMOVED": 1},
        "changes_by_severity": {"high": 1, "medium": 0, "low": 0},
    }
    assert bump["usage"] == {
        "status": "ok",
        "files_scanned": 2,
        "files_skipped": 1,
        "hit_count": 0,
        "risk_flags": ["dynamic_import_unknown_target"],
    }


def test_worker_reads_requires_python_from_snapshot_metadata(tmp_path: Path, monkeypatch) -> None:
    root = snapshot_root(tmp_path, requires_python=">=3.9")
    verdict = make_verdict()
    monkeypatch.setattr(eval_worker, "run_check", fake_check((verdict,)))

    result = eval_worker.run_case(worker_payload(root))

    requires_python = result["diagnostics"]["requires_python"]
    assert requires_python["package"] == "pkg"
    assert requires_python["old"] == ">=3.9"
    assert requires_python["new"] == ">=3.9"
    assert requires_python["state"] == "available"
    assert requires_python["project"]["source"] == "unknown"


def test_worker_requires_python_null_when_metadata_missing_or_blank(
    tmp_path: Path, monkeypatch
) -> None:
    verdict = make_verdict()
    monkeypatch.setattr(eval_worker, "run_check", fake_check((verdict,)))

    missing = eval_worker.run_case(
        worker_payload(snapshot_root(tmp_path / "missing", include_metadata_file=False))
    )
    blank = eval_worker.run_case(
        worker_payload(snapshot_root(tmp_path / "blank", blank_requires_python=True))
    )

    missing_requires_python = missing["diagnostics"]["requires_python"]
    blank_requires_python = blank["diagnostics"]["requires_python"]
    for diagnostics in (missing_requires_python, blank_requires_python):
        assert diagnostics["package"] == "pkg"
        assert diagnostics["old"] is None
        assert diagnostics["new"] is None
        assert diagnostics["state"] == "available"


def test_worker_persists_requires_python_project_and_excluded_versions(
    tmp_path: Path, monkeypatch
) -> None:
    root = snapshot_root(tmp_path)
    (root / "old" / "metadata.json").write_text(
        json.dumps({"info": {"requires_python": ">=3.8"}}), encoding="utf-8"
    )
    (root / "new" / "metadata.json").write_text(
        json.dumps({"info": {"requires_python": ">=3.10"}}), encoding="utf-8"
    )
    workflow = root / "repo" / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        """
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.8", "3.9", "3.10"]
""",
        encoding="utf-8",
    )
    api = make_api([]).model_copy(
        update={
            "old_requires_python": ">=3.8",
            "new_requires_python": ">=3.10",
        }
    )
    verdict = make_verdict(rule_id="R8", verdict="LIKELY_BREAKING")
    monkeypatch.setattr(
        eval_worker,
        "run_check",
        fake_check((verdict,), apis={id(verdict): api}),
    )

    result = eval_worker.run_case(worker_payload(root))
    requires_python = result["diagnostics"]["requires_python"]

    assert requires_python["old"] == ">=3.8"
    assert requires_python["new"] == ">=3.10"
    assert requires_python["project"]["source"] == "ci_matrix"
    assert requires_python["project"]["status"] == "explicit"
    assert requires_python["project"]["versions"] == ["3.8", "3.9", "3.10"]
    assert requires_python["assessment"]["tightened"] is True
    assert "3.8" in requires_python["assessment"]["excluded"]
    assert "3.9" in requires_python["assessment"]["excluded"]


def test_worker_enriches_no_usage_evidence_with_change_kind_and_parameter(
    tmp_path: Path, monkeypatch
) -> None:
    changes = [
        ApiChange(
            kind="PARAMETER_REMOVED",
            symbol="pkg.Client",
            severity="high",
            detail="a removed",
            parameter="a",
        ),
        ApiChange(
            kind="PARAMETER_REMOVED",
            symbol="pkg.Client",
            severity="high",
            detail="b removed",
            parameter="b",
        ),
        ApiChange(
            kind="OBJECT_REMOVED",
            symbol="pkg.Client",
            severity="medium",
            detail="object removed",
        ),
    ]
    api = make_api(changes)
    verdict = make_verdict(
        evidence=tuple(
            Evidence(
                kind="reason", summary=f"No usage found for {change.symbol}", symbol=change.symbol
            )
            for change in changes
        )
    )
    monkeypatch.setattr(
        eval_worker,
        "run_check",
        fake_check((verdict,), apis={id(verdict): api}),
    )

    result = eval_worker.run_case(worker_payload(snapshot_root(tmp_path)))

    assert [item["summary"] for item in result["evidence_refs"]] == [
        "No usage found for pkg.Client (PARAMETER_REMOVED: a, b; OBJECT_REMOVED)"
    ]


def test_worker_dedupes_evidence_refs_preserving_order(tmp_path: Path, monkeypatch) -> None:
    change = ApiChange(
        kind="PARAMETER_REMOVED",
        symbol="pkg.Client",
        severity="high",
        detail="removed timeout",
        parameter="timeout",
    )
    api = make_api([change])
    reason = Evidence(kind="reason", summary="No usage found for pkg.Client", symbol="pkg.Client")
    hit = Evidence(
        kind="usage_hit",
        summary="call pkg.Client",
        file="app.py",
        line=3,
        symbol="pkg.Client",
    )
    verdicts = (make_verdict(evidence=(reason, hit)), make_verdict(evidence=(reason, hit)))
    monkeypatch.setattr(
        eval_worker,
        "run_check",
        fake_check(verdicts, apis={id(verdict): api for verdict in verdicts}),
    )

    result = eval_worker.run_case(worker_payload(snapshot_root(tmp_path)))

    summaries = [item["summary"] for item in result["evidence_refs"]]
    assert len(summaries) == 2
    assert summaries[0] == "No usage found for pkg.Client (PARAMETER_REMOVED: timeout)"
    assert summaries[1] == "call pkg.Client"


def test_worker_diagnostics_empty_for_incomplete_snapshot(tmp_path: Path) -> None:
    root = snapshot_root(tmp_path, status="failed")

    result = eval_worker.run_case(worker_payload(root))

    assert result["snapshot_status"] == "failed"
    assert result["error_kind"] == "failed"
    assert result["diagnostics"]["snapshot"] == {"status": "failed", "repo_present": True}
    assert result["diagnostics"]["bumps"] == []


def test_no_usage_evidence_enrichment_matches_real_rules_output() -> None:
    changes = [
        ApiChange(
            kind="PARAMETER_REMOVED",
            symbol="pkg.Client",
            severity="medium",
            detail="m removed",
            parameter="m",
        ),
        ApiChange(
            kind="OBJECT_REMOVED",
            symbol="pkg.Other",
            severity="low",
            detail="object removed",
        ),
    ]
    api = make_api(changes)
    usage = UsageResult(package="pkg", symbols=["pkg.Client", "pkg.Other"], files_scanned=1)
    bump = Bump(
        package="pkg",
        old_version="1.0.0",
        new_version="2.0.0",
        source="uv.lock",
    )

    verdict = decide(bump, api, usage)
    refs = eval_worker._build_evidence_refs((verdict,), {id(verdict): (api, usage)})

    assert verdict.rule_id == "R7"
    assert [item["summary"] for item in refs] == [
        "No usage found for pkg.Client (PARAMETER_REMOVED: m)",
        "No usage found for pkg.Other (OBJECT_REMOVED)",
    ]
