from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bumpcheck import cli
from bumpcheck.apidiff import ApiChange, ApiDiffResult, Location
from bumpcheck.check import check
from bumpcheck.cli import app
from bumpcheck.usage import UsageHit, UsageResult
from bumpcheck.verdict import BumpVerdict, CheckResult, Evidence


def uv_lock(*packages: tuple[str, str]) -> str:
    return "".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n\n' for name, version in packages
    )


def make_api(
    package: str = "pkg",
    *,
    changes: list[ApiChange] | None = None,
    status: str = "ok",
    reason: str | None = None,
    top_level: list[str] | None = None,
) -> ApiDiffResult:
    return ApiDiffResult(
        package=package,
        old_version="1.0.0",
        new_version="2.0.0",
        status=status,
        reason=reason,
        changes=changes or [],
        top_level=top_level if top_level is not None else [package],
    )


def make_change(
    kind: str = "PARAMETER_REMOVED",
    symbol: str = "pkg.Client",
    *,
    severity: str = "high",
    parameter: str | None = "timeout",
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
    keywords: tuple[str, ...] = ("timeout",),
) -> UsageHit:
    return UsageHit(
        symbol=symbol,
        file="app.py",
        line=3,
        col=0,
        snippet="...",
        kind=kind,
        confidence=confidence,
        keywords=keywords,
    )


def make_usage(
    hits: list[UsageHit] | None = None,
    *,
    risks: list[str] | None = None,
    package: str = "pkg",
) -> UsageResult:
    collected = hits or []
    symbols = sorted({hit.symbol for hit in collected}) or [f"{package}.Client"]
    return UsageResult(
        package=package,
        symbols=symbols,
        hits=collected,
        files_scanned=1,
        risks=risks or [],
    )


def test_check_r2_does_not_trace_usage(tmp_path: Path) -> None:
    def fake_diff(package: str, old: str, new: str, cache_dir: Path | None = None):
        return make_api(package)

    def fake_usage(*args: object, **kwargs: object) -> UsageResult:
        raise AssertionError("usage should not run when there are no changes")

    result = check(
        tmp_path,
        uv_lock(("pkg", "1.0.0")),
        uv_lock(("pkg", "1.0.1")),
        "uv.lock",
        diff_fn=fake_diff,
        usage_fn=fake_usage,
    )

    assert result.overall == "SAFE"
    assert result.bumps[0].verdict == "SAFE"
    assert result.bumps[0].rule_id == "R2"


def test_check_passes_import_names_symbols_parameters_and_cache_dir(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_diff(package: str, old: str, new: str, cache_dir: Path | None = None):
        captured["diff"] = (package, old, new, cache_dir)
        return make_api(package, changes=[make_change()], top_level=["pkg", "pkg_extra"])

    def fake_usage(
        repo_path: Path,
        package: str,
        symbols: list[str],
        *,
        import_names: list[str],
        parameters: dict[str, list[tuple[str, str]]],
    ) -> UsageResult:
        captured["usage"] = (repo_path, package, symbols, import_names, parameters)
        return make_usage([make_hit()])

    cache_dir = tmp_path / "cache"
    result = check(
        tmp_path,
        uv_lock(("pkg", "1.0.0")),
        uv_lock(("pkg", "2.0.0")),
        "uv.lock",
        diff_fn=fake_diff,
        usage_fn=fake_usage,
        cache_dir=cache_dir,
    )

    assert captured["diff"] == ("pkg", "1.0.0", "2.0.0", cache_dir)
    assert captured["usage"] == (
        tmp_path,
        "pkg",
        ["pkg.Client"],
        ["pkg", "pkg_extra"],
        {"pkg.Client": [("timeout", "PARAMETER_REMOVED")]},
    )
    assert result.overall == "LIKELY_BREAKING"
    assert result.bumps[0].rule_id == "R4"


def test_check_unresolved_versions_are_unknown_r0(tmp_path: Path) -> None:
    def fake_diff(*args: object, **kwargs: object) -> ApiDiffResult:
        raise AssertionError("unresolved packages must not be diffed")

    result = check(
        tmp_path,
        uv_lock(("pkg", "1.0.0"), ("pkg", "1.1.0")),
        uv_lock(("pkg", "2.0.0")),
        "uv.lock",
        diff_fn=fake_diff,
    )

    assert result.overall == "UNKNOWN"
    assert result.bumps[0].verdict == "UNKNOWN"
    assert result.bumps[0].rule_id == "R0"


def test_check_overall_aggregation_safe_plus_unknown_is_unknown(tmp_path: Path) -> None:
    def fake_diff(package: str, old: str, new: str, cache_dir: Path | None = None):
        if package == "alpha":
            return make_api(package, status="no_api_diff", reason="no_wheel")
        return make_api(package)

    result = check(
        tmp_path,
        uv_lock(("alpha", "1.0.0"), ("beta", "1.0.0")),
        uv_lock(("alpha", "2.0.0"), ("beta", "1.0.1")),
        "uv.lock",
        diff_fn=fake_diff,
    )

    assert result.overall == "UNKNOWN"
    assert {(bump.package, bump.rule_id) for bump in result.bumps} == {
        ("alpha", "R1"),
        ("beta", "R2"),
    }


def test_check_e1_isolation_keeps_other_bumps(tmp_path: Path) -> None:
    def fake_diff(package: str, old: str, new: str, cache_dir: Path | None = None):
        if package == "alpha":
            raise RuntimeError("boom")
        return make_api(package)

    result = check(
        tmp_path,
        uv_lock(("alpha", "1.0.0"), ("beta", "1.0.0")),
        uv_lock(("alpha", "2.0.0"), ("beta", "1.0.1")),
        "uv.lock",
        diff_fn=fake_diff,
    )

    assert result.overall == "UNKNOWN"
    alpha = next(bump for bump in result.bumps if bump.package == "alpha")
    beta = next(bump for bump in result.bumps if bump.package == "beta")
    assert alpha.verdict == "UNKNOWN"
    assert alpha.rule_id == "E1"
    assert "RuntimeError: boom" in alpha.rationale
    assert beta.verdict == "SAFE"
    assert beta.rule_id == "R2"


def test_check_usage_exception_is_isolated(tmp_path: Path) -> None:
    def fake_diff(package: str, old: str, new: str, cache_dir: Path | None = None):
        return make_api(package, changes=[make_change()])

    def fake_usage(*args: object, **kwargs: object) -> UsageResult:
        raise ValueError("bad trace")

    result = check(
        tmp_path,
        uv_lock(("pkg", "1.0.0")),
        uv_lock(("pkg", "2.0.0")),
        "uv.lock",
        diff_fn=fake_diff,
        usage_fn=fake_usage,
    )

    assert result.bumps[0].verdict == "UNKNOWN"
    assert result.bumps[0].rule_id == "E1"
    assert "ValueError: bad trace" in result.bumps[0].rationale


def test_check_is_deterministic(tmp_path: Path) -> None:
    def fake_diff(package: str, old: str, new: str, cache_dir: Path | None = None):
        return make_api(package, changes=[make_change()], top_level=["pkg"])

    def fake_usage(*args: object, **kwargs: object) -> UsageResult:
        return make_usage([make_hit()])

    kwargs = {"diff_fn": fake_diff, "usage_fn": fake_usage}
    first = check(
        tmp_path,
        uv_lock(("pkg", "1.0.0")),
        uv_lock(("pkg", "2.0.0")),
        "uv.lock",
        **kwargs,
    )
    second = check(
        tmp_path,
        uv_lock(("pkg", "1.0.0")),
        uv_lock(("pkg", "2.0.0")),
        "uv.lock",
        **kwargs,
    )

    assert first.model_dump_json(indent=2) == second.model_dump_json(indent=2)


def _check_result(verdict: str) -> CheckResult:
    bump = BumpVerdict(
        package="pkg",
        old_version="1.0.0",
        new_version="2.0.0",
        verdict=verdict,
        rule_id="R2" if verdict == "SAFE" else "R5",
        rationale="rationale",
        evidence=(
            Evidence(
                kind="usage_hit",
                summary="call pkg.Client",
                file="app.py",
                line=3,
                symbol="pkg.Client",
            ),
        ),
    )
    return CheckResult(overall=verdict, bumps=(bump,))


def _invoke_check(tmp_path: Path, *extra: str):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    old = tmp_path / "old.lock"
    new = tmp_path / "new.lock"
    old.write_text(uv_lock(("pkg", "1.0.0")), encoding="utf-8")
    new.write_text(uv_lock(("pkg", "2.0.0")), encoding="utf-8")
    return CliRunner().invoke(
        app,
        ["check", str(repo), str(old), str(new), "--kind", "uv.lock", *extra],
    )


def test_cli_check_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_check", lambda *args, **kwargs: _check_result("SAFE"))

    result = _invoke_check(tmp_path)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["overall"] == "SAFE"
    assert payload["bumps"][0]["rule_id"] == "R2"


def test_cli_check_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_check", lambda *args, **kwargs: _check_result("SAFE"))

    result = _invoke_check(tmp_path, "--format", "text")

    assert result.exit_code == 0
    assert "SAFE pkg 1.0.0->2.0.0 [R2] rationale" in result.output
    assert "  app.py:3 call pkg.Client" in result.output
    assert result.output.strip().endswith("overall: SAFE")


def test_cli_check_fail_on_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_check", lambda *args, **kwargs: _check_result("REVIEW"))

    at_threshold = _invoke_check(tmp_path, "--fail-on", "REVIEW")
    above_threshold = _invoke_check(tmp_path, "--fail-on", "LIKELY_BREAKING")

    assert at_threshold.exit_code == 1
    assert above_threshold.exit_code == 0


def test_cli_check_fail_on_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_check", lambda *args, **kwargs: _check_result("UNKNOWN"))

    result = _invoke_check(tmp_path, "--fail-on", "UNKNOWN")

    assert result.exit_code == 1


def test_cli_check_parse_error_exits_2(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    old = tmp_path / "old.lock"
    new = tmp_path / "new.lock"
    old.write_text("not a lockfile", encoding="utf-8")
    new.write_text("not a lockfile", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["check", str(repo), str(old), str(new), "--kind", "uv.lock"],
    )

    assert result.exit_code == 2


def test_check_on_bump_receives_api_and_usage_diagnostics(tmp_path: Path) -> None:
    api = make_api(changes=[make_change()])
    usage = make_usage([make_hit()])
    events: list[tuple[Any, Any, Any]] = []

    def on_bump(verdict: Any, api_value: Any, usage_value: Any) -> None:
        events.append((verdict, api_value, usage_value))

    result = check(
        tmp_path,
        uv_lock(("pkg", "1.0.0")),
        uv_lock(("pkg", "2.0.0")),
        "uv.lock",
        diff_fn=lambda *args, **kwargs: api,
        usage_fn=lambda *args, **kwargs: usage,
        on_bump=on_bump,
    )

    assert len(events) == 1
    assert events[0][0] is result.bumps[0]
    assert events[0][1] is api
    assert events[0][2] is usage


def test_check_on_bump_receives_partial_diagnostics_on_usage_error(tmp_path: Path) -> None:
    api = make_api(changes=[make_change()])
    events: list[tuple[Any, Any, Any]] = []

    def on_bump(verdict: Any, api_value: Any, usage_value: Any) -> None:
        events.append((verdict, api_value, usage_value))

    def failing_usage(*args: object, **kwargs: object) -> UsageResult:
        raise RuntimeError("boom")

    result = check(
        tmp_path,
        uv_lock(("pkg", "1.0.0")),
        uv_lock(("pkg", "2.0.0")),
        "uv.lock",
        diff_fn=lambda *args, **kwargs: api,
        usage_fn=failing_usage,
        on_bump=on_bump,
    )

    assert len(events) == 1
    assert events[0][0] is result.bumps[0]
    assert events[0][0].rule_id == "E1"
    assert events[0][1] is api
    assert events[0][2] is None
