from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from bumpcheck.apidiff import ApiChange, ApiDiffResult

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "apidiff_acceptance.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("apidiff_acceptance_script", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


acceptance = _load_module()


def _change(symbol: str, kind: str, parameter: str | None = None, severity: str = "high"):
    return ApiChange(
        kind=kind, symbol=symbol, severity=severity, detail="detail", parameter=parameter
    )


def _result(
    package: str,
    old: str,
    new: str,
    *,
    status: str = "ok",
    reason: str | None = None,
    changes: tuple = (),
) -> ApiDiffResult:
    return ApiDiffResult(
        package=package,
        old_version=old,
        new_version=new,
        status=status,
        reason=reason,
        changes=list(changes),
    )


def _pair(package: str, expect: list[dict]) -> dict:
    return {
        "package": package,
        "old": "1.0.0",
        "new": "2.0.0",
        "expect": expect,
        "note": f"{package} note",
        "source": f"https://example.com/{package}",
    }


def _write_pairs(tmp_path: Path, pairs: list[dict]) -> Path:
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(pairs), encoding="utf-8")
    return path


def _fake_fetcher(mapping: dict) -> object:
    calls: list[tuple] = []

    def fake(name, old, new, cache_dir=None, fetcher=None):
        calls.append((name, old, new))
        outcome = mapping[(name, old, new)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    fake.calls = calls
    return fake


def _run(tmp_path: Path, pairs: list[dict], mapping: dict, **kwargs):
    monkeypatch_fake = _fake_fetcher(mapping)
    acceptance.diff_versions = monkeypatch_fake
    pairs_path = _write_pairs(tmp_path, pairs)
    report = acceptance.run(
        pairs_path,
        cache_dir=tmp_path / "cache",
        report_md=tmp_path / "report.md",
        report_json=tmp_path / "report.json",
        **kwargs,
    )
    return report, monkeypatch_fake


def _expectation(
    symbol: str,
    result: str,
    *,
    known_miss: bool = False,
    known_miss_reason: str | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "kind": None,
        "parameter": None,
        "result": result,
        "known_miss": known_miss,
        "known_miss_reason": known_miss_reason,
    }


def _pair_result(
    package: str,
    status: str = "ok",
    *,
    expectations: tuple[dict, ...] = (),
    reason: str | None = None,
    error: str | None = None,
) -> dict:
    return {
        "package": package,
        "old": "1.0.0",
        "new": "2.0.0",
        "status": status,
        "reason": reason,
        "error": error,
        "expectations": list(expectations),
    }


def _report(pairs: list[dict]) -> dict:
    expectations = [item for pair in pairs for item in pair["expectations"]]
    hits = sum(item["result"] == "HIT" for item in expectations)
    misses = sum(item["result"] == "MISS" for item in expectations)
    known_misses = sum(
        item.get("known_miss", False) and item["result"] != "HIT" for item in expectations
    )
    errors = sum(pair["status"] == "error" for pair in pairs)
    no_api_diff: dict[str, int] = {}
    for pair in pairs:
        if pair["status"] == "no_api_diff":
            reason = pair["reason"] or "unknown"
            no_api_diff[reason] = no_api_diff.get(reason, 0) + 1
    total = len(expectations)
    return {
        "summary": {
            "pairs": len(pairs),
            "expectations": total,
            "hits": hits,
            "misses": misses,
            "known_misses": known_misses,
            "errors": errors,
            "no_api_diff": no_api_diff,
            "recall": hits / total if total else None,
        },
        "pairs": pairs,
    }


def _run_main(monkeypatch, report: dict) -> int:
    monkeypatch.setattr(acceptance, "run", lambda *args, **kwargs: report)
    return acceptance.main(["run"])


def test_hit_miss_no_api_diff_and_error(tmp_path: Path) -> None:
    pairs = [
        _pair(
            "pkg-a",
            [{"symbol": "pkg_a.foo", "kind": "PARAMETER_REMOVED", "parameter": "label"}],
        ),
        _pair("pkg-b", [{"symbol": "pkg_b.foo", "kind": "PARAMETER_REMOVED", "parameter": None}]),
        _pair("pkg-c", [{"symbol": "pkg_c.missing", "kind": None, "parameter": None}]),
        _pair("pkg-d", [{"symbol": "pkg_d.foo", "kind": None, "parameter": None}]),
        _pair("pkg-e", [{"symbol": "pkg_e.foo", "kind": None, "parameter": None}]),
    ]
    mapping = {
        ("pkg-a", "1.0.0", "2.0.0"): _result(
            "pkg-a",
            "1.0.0",
            "2.0.0",
            changes=(_change("pkg_a.foo", "PARAMETER_REMOVED", "label"),),
        ),
        ("pkg-b", "1.0.0", "2.0.0"): _result(
            "pkg-b",
            "1.0.0",
            "2.0.0",
            changes=(_change("pkg_b.foo", "PARAMETER_CHANGED_DEFAULT"),),
        ),
        ("pkg-c", "1.0.0", "2.0.0"): _result(
            "pkg-c", "1.0.0", "2.0.0", changes=(_change("pkg_c.other", "OBJECT_REMOVED"),)
        ),
        ("pkg-d", "1.0.0", "2.0.0"): _result(
            "pkg-d", "1.0.0", "2.0.0", status="no_api_diff", reason="no_wheel"
        ),
        ("pkg-e", "1.0.0", "2.0.0"): RuntimeError("boom"),
    }

    report, fake = _run(tmp_path, pairs, mapping)

    assert len(fake.calls) == 5
    summary = report["summary"]
    assert summary["pairs"] == 5
    assert summary["expectations"] == 5
    assert summary["hits"] == 1
    assert summary["misses"] == 2
    assert summary["errors"] == 1
    assert summary["no_api_diff"] == {"no_wheel": 1}

    by_package = {pair["package"]: pair for pair in report["pairs"]}
    assert by_package["pkg-a"]["expectations"][0]["result"] == "HIT"
    assert by_package["pkg-a"]["top_changes"] == [
        {
            "kind": "PARAMETER_REMOVED",
            "symbol": "pkg_a.foo",
            "severity": "high",
            "parameter": "label",
        }
    ]
    assert by_package["pkg-b"]["expectations"][0]["result"] == "MISS"
    assert by_package["pkg-c"]["expectations"][0]["result"] == "MISS"
    assert by_package["pkg-d"]["status"] == "no_api_diff"
    assert by_package["pkg-d"]["reason"] == "no_wheel"
    assert by_package["pkg-d"]["expectations"][0]["result"] == "NO_API_DIFF"
    assert by_package["pkg-e"]["status"] == "error"
    assert by_package["pkg-e"]["error"] == "RuntimeError: boom"
    assert by_package["pkg-e"]["expectations"][0]["result"] == "ERROR"

    on_disk = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert on_disk["summary"] == summary
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Summary" in markdown
    assert "pkg-a 1.0.0 -> 2.0.0" in markdown
    assert "| pkg_a.foo | PARAMETER_REMOVED | label | HIT |  |  |" in markdown


def test_top_changes_are_capped_and_ranked(tmp_path: Path) -> None:
    pairs = [_pair("pkg-a", [{"symbol": "pkg_a.foo", "kind": None, "parameter": None}])]
    mapping = {
        ("pkg-a", "1.0.0", "2.0.0"): _result(
            "pkg-a",
            "1.0.0",
            "2.0.0",
            changes=(
                _change("pkg_a.low", "ATTRIBUTE_CHANGED_VALUE", severity="low"),
                _change("pkg_a.high", "OBJECT_REMOVED", severity="high"),
                _change("pkg_a.med", "RETURN_CHANGED_TYPE", severity="medium"),
                _change("pkg_a.high2", "PARAMETER_REMOVED", severity="high"),
            ),
        )
    }

    report, _ = _run(tmp_path, pairs, mapping)

    assert [change["symbol"] for change in report["pairs"][0]["top_changes"]] == [
        "pkg_a.high",
        "pkg_a.high2",
        "pkg_a.med",
    ]


def test_only_filters_pairs_and_canonicalizes(tmp_path: Path) -> None:
    pairs = [
        _pair("pkg-a", [{"symbol": "pkg_a.foo", "kind": None, "parameter": None}]),
        _pair("pkg-b", [{"symbol": "pkg_b.foo", "kind": None, "parameter": None}]),
    ]
    mapping = {
        ("pkg-a", "1.0.0", "2.0.0"): _result("pkg-a", "1.0.0", "2.0.0"),
        ("pkg-b", "1.0.0", "2.0.0"): _result("pkg-b", "1.0.0", "2.0.0"),
    }

    report, fake = _run(tmp_path, pairs, mapping, only="Pkg_A")

    assert [call[0] for call in fake.calls] == ["pkg-a"]
    assert report["summary"]["pairs"] == 1
    assert report["pairs"][0]["package"] == "pkg-a"


def test_missing_pairs_file_is_invalid(tmp_path: Path) -> None:
    acceptance.diff_versions = _fake_fetcher({})
    assert acceptance.main(["run", "--pairs", str(tmp_path / "missing.json")]) == 2


def test_malformed_pairs_json_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "pairs.json"
    path.write_text("{not json", encoding="utf-8")
    acceptance.diff_versions = _fake_fetcher({})
    assert acceptance.main(["run", "--pairs", str(path)]) == 2


def test_wrong_shape_pairs_file_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps({"package": "idna"}), encoding="utf-8")
    with pytest.raises(acceptance.PairsError):
        acceptance.load_pairs(path)
    acceptance.diff_versions = _fake_fetcher({})
    assert acceptance.main(["run", "--pairs", str(path)]) == 2


def test_main_returns_zero_on_success(monkeypatch, capsys) -> None:
    report = _report([_pair_result("pkg-a", expectations=(_expectation("pkg_a.foo", "HIT"),))])

    assert _run_main(monkeypatch, report) == 0
    assert "recall=1/1 (1.000)" in capsys.readouterr().out


def test_known_miss_does_not_fail_exit_code(monkeypatch, capsys) -> None:
    report = _report(
        [
            _pair_result(
                "pkg-a",
                expectations=(
                    _expectation(
                        "pkg_a.gone",
                        "MISS",
                        known_miss=True,
                        known_miss_reason="changelog confirms removal",
                    ),
                ),
            )
        ]
    )

    assert _run_main(monkeypatch, report) == 0
    output = capsys.readouterr().out
    assert "pkg_a.gone -: MISS [known_miss]" in output
    assert "known_misses=1" in output


def test_known_miss_counts_against_recall(monkeypatch, capsys) -> None:
    report = _report(
        [
            _pair_result(
                "pkg-a",
                expectations=(
                    _expectation("pkg_a.hit", "HIT"),
                    _expectation("pkg_a.gone", "MISS", known_miss=True),
                ),
            )
        ]
    )

    assert _run_main(monkeypatch, report) == 0
    output = capsys.readouterr().out
    assert "hits=1 misses=1 known_misses=1 errors=0 no_api_diff=0 recall=1/2 (0.500)" in output


def test_unexpected_miss_exits_one(monkeypatch, capsys) -> None:
    report = _report([_pair_result("pkg-a", expectations=(_expectation("pkg_a.gone", "MISS"),))])

    assert _run_main(monkeypatch, report) == 1
    output = capsys.readouterr().out
    assert "hits=0 misses=1 known_misses=0 errors=0 no_api_diff=0" in output


def test_known_miss_now_hit_prints_note(monkeypatch, capsys) -> None:
    report = _report(
        [
            _pair_result(
                "pkg-a",
                expectations=(
                    _expectation(
                        "pkg_a.gone",
                        "HIT",
                        known_miss=True,
                        known_miss_reason="fixed upstream",
                    ),
                ),
            )
        ]
    )

    assert _run_main(monkeypatch, report) == 0
    output = capsys.readouterr().out
    assert "marked known_miss but now HITs: fixed upstream" in output
    assert "known_misses=0" in output


def test_error_and_no_api_diff_reported_separately_from_misses(monkeypatch, capsys) -> None:
    report = _report(
        [
            _pair_result(
                "pkg-error",
                "error",
                expectations=(_expectation("pkg_error.foo", "ERROR"),),
                error="RuntimeError: boom",
            ),
            _pair_result(
                "pkg-no-wheel",
                "no_api_diff",
                expectations=(_expectation("pkg_no_wheel.foo", "NO_API_DIFF"),),
                reason="no_wheel",
            ),
            _pair_result(
                "pkg-miss",
                expectations=(_expectation("pkg_miss.foo", "MISS"),),
            ),
        ]
    )

    assert _run_main(monkeypatch, report) == 1
    output = capsys.readouterr().out
    assert "hits=0 misses=1 known_misses=0 errors=1 no_api_diff=1" in output
    assert "error: RuntimeError: boom" in output
    assert "reason: no_wheel" in output
