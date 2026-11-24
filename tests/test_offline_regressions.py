"""Offline regression fixtures.

The fixtures are hand-written diffs. They prove the rule engine, NOT that
Griffe produces these diffs from real wheels.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bumpcheck import cli
from bumpcheck.apidiff import ApiDiffResult
from bumpcheck.check import check
from bumpcheck.cli import app
from bumpcheck.projectpython import ProjectPythonSet
from bumpcheck.usage import UsageResult

FIXTURES = Path(__file__).parent / "fixtures" / "offline_regressions"


def uv_lock(*packages: tuple[str, str]) -> str:
    return "".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n\n' for name, version in packages
    )


def api_diff(case: str) -> ApiDiffResult:
    path = FIXTURES / case / "diff.json"
    return ApiDiffResult.model_validate_json(path.read_text(encoding="utf-8"))


def run_offline(
    repo: Path,
    api: ApiDiffResult,
    *,
    project: ProjectPythonSet | None = None,
    on_bump: Any = None,
):
    return check(
        repo,
        uv_lock((api.package, api.old_version)),
        uv_lock((api.package, api.new_version)),
        "uv.lock",
        diff_fn=lambda *_args, **_kwargs: api,
        project_python_fn=lambda _: project or ProjectPythonSet(source="unknown", status="unknown"),
        on_bump=on_bump,
    )


def source_repo(tmp_path: Path, source: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(source, encoding="utf-8")
    return repo


def capture_usage(target: dict[str, UsageResult | None]):
    def capture(_verdict: object, _api: object, usage: UsageResult | None) -> None:
        target["usage"] = usage

    return capture


def test_idna_removed_label_is_r4_likely_breaking_with_exact_keyword() -> None:
    api = api_diff("idna_removed_label")
    repo = FIXTURES / "idna_removed_label" / "repo"
    captured: dict[str, UsageResult | None] = {}

    result = run_offline(repo, api, on_bump=capture_usage(captured))
    bump = result.bumps[0]
    usage = captured["usage"]

    assert result.overall == "LIKELY_BREAKING"
    assert bump.verdict == "LIKELY_BREAKING"
    assert bump.rule_id == "R4"
    assert bump.api_status == "ok"
    assert (bump.package, bump.old_version, bump.new_version) == ("idna", "3.11", "3.15")
    assert any(
        evidence.kind == "api_change"
        and evidence.summary == "PARAMETER_REMOVED idna.core.valid_string_length (label)"
        for evidence in bump.evidence
    )
    assert any(
        evidence.kind == "usage_hit" and evidence.file == "app.py" for evidence in bump.evidence
    )
    assert usage is not None
    assert usage.parameters == {"idna.core.valid_string_length": [("label", "PARAMETER_REMOVED")]}
    assert usage.files_scanned >= 1
    assert any(
        hit.kind == "call" and hit.confidence == "exact" and "label" in hit.keywords
        for hit in usage.hits
        if hit.symbol == "idna.core.valid_string_length"
    )


def test_idna_removed_label_without_keyword_is_r5_review(tmp_path: Path) -> None:
    api = api_diff("idna_removed_label")
    repo = source_repo(
        tmp_path,
        "from idna.core import valid_string_length\n"
        '\nvalid_string_length("example.com", domain=1)\n',
    )
    captured: dict[str, UsageResult | None] = {}

    result = run_offline(repo, api, on_bump=capture_usage(captured))
    bump = result.bumps[0]
    usage = captured["usage"]

    assert result.overall == "REVIEW"
    assert bump.verdict == "REVIEW"
    assert bump.rule_id == "R5"
    assert usage is not None
    assert any(
        hit.kind == "call"
        and hit.confidence == "exact"
        and "domain" in hit.keywords
        and "label" not in hit.keywords
        for hit in usage.hits
        if hit.symbol == "idna.core.valid_string_length"
    )


def test_idna_removed_label_positional_call_is_r5_review(tmp_path: Path) -> None:
    api = api_diff("idna_removed_label")
    repo = source_repo(
        tmp_path,
        'from idna.core import valid_string_length\n\nvalid_string_length("example.com", 1)\n',
    )
    captured: dict[str, UsageResult | None] = {}

    result = run_offline(repo, api, on_bump=capture_usage(captured))
    bump = result.bumps[0]
    usage = captured["usage"]

    assert result.overall == "REVIEW"
    assert bump.verdict == "REVIEW"
    assert bump.rule_id == "R5"
    assert usage is not None
    assert any(
        hit.kind == "call"
        and hit.confidence == "exact"
        and hit.n_positional == 2
        and hit.keywords == ()
        for hit in usage.hits
        if hit.symbol == "idna.core.valid_string_length"
    )


def test_python_version_support_is_r8_likely_breaking(tmp_path: Path) -> None:
    api = api_diff("python_version_support")
    project = ProjectPythonSet(
        source="ci_matrix",
        status="explicit",
        versions=("3.8", "3.9", "3.10"),
    )

    result = run_offline(tmp_path, api, project=project)
    bump = result.bumps[0]
    summaries = {evidence.summary for evidence in bump.evidence}

    assert result.overall == "LIKELY_BREAKING"
    assert bump.verdict == "LIKELY_BREAKING"
    assert bump.rule_id == "R8"
    assert "requires_python old: >=3.8" in summaries
    assert "requires_python new: >=3.10" in summaries
    assert "excluded Python versions: 3.8, 3.9" in summaries


def test_no_matching_usage_is_r7_safe_with_successful_scan(tmp_path: Path) -> None:
    api = api_diff("idna_removed_label")
    repo = source_repo(tmp_path, "value = 1\n")
    captured: dict[str, UsageResult | None] = {}

    result = run_offline(repo, api, on_bump=capture_usage(captured))
    bump = result.bumps[0]
    usage = captured["usage"]

    assert result.overall == "SAFE"
    assert bump.verdict == "SAFE"
    assert bump.rule_id == "R7"
    assert usage is not None
    assert usage.files_scanned == 1
    assert not any(evidence.kind == "usage_hit" for evidence in bump.evidence)


def test_zero_scan_is_r9_unknown(tmp_path: Path) -> None:
    api = api_diff("idna_removed_label")
    captured: dict[str, UsageResult | None] = {}

    result = run_offline(tmp_path, api, on_bump=capture_usage(captured))
    bump = result.bumps[0]
    usage = captured["usage"]

    assert result.overall == "UNKNOWN"
    assert bump.verdict == "UNKNOWN"
    assert bump.rule_id == "R9"
    assert usage is not None
    assert usage.files_scanned == 0
    assert "files_scanned=0; files_skipped=0" in {evidence.summary for evidence in bump.evidence}


def test_no_api_diff_is_r1_unknown(tmp_path: Path) -> None:
    api = api_diff("no_api_diff")
    captured: dict[str, UsageResult | None] = {}

    result = run_offline(tmp_path, api, on_bump=capture_usage(captured))
    bump = result.bumps[0]

    assert result.overall == "UNKNOWN"
    assert bump.verdict == "UNKNOWN"
    assert bump.rule_id == "R1"
    assert bump.api_status == "no_api_diff"
    assert "no_wheel" in bump.rationale
    assert captured["usage"] is None


def test_cli_idna_check_fail_on_likely_breaking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = api_diff("idna_removed_label")
    repo = source_repo(
        tmp_path,
        "from idna.core import valid_string_length\n"
        '\nvalid_string_length("example.com", label=1)\n',
    )
    old = tmp_path / "old.lock"
    new = tmp_path / "new.lock"
    old.write_text(uv_lock(("idna", "3.11")), encoding="utf-8")
    new.write_text(uv_lock(("idna", "3.15")), encoding="utf-8")

    def offline_run_check(*args: Any, **kwargs: Any):
        return check(*args, diff_fn=lambda *_args, **_kwargs: api, **kwargs)

    monkeypatch.setattr(cli, "run_check", offline_run_check)
    result = CliRunner().invoke(
        app,
        [
            "check",
            str(repo),
            str(old),
            str(new),
            "--kind",
            "uv.lock",
            "--fail-on",
            "LIKELY_BREAKING",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["overall"] == "LIKELY_BREAKING"
    assert payload["bumps"][0]["rule_id"] == "R4"
