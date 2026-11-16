"""Acceptance harness for the API diff: verify known breakages in real releases.

Dev-only helper; it is not part of the installed package. The ``run`` command
fetches wheels from PyPI through :mod:`bumpcheck.apidiff` (wheels only, never
installs or imports them), checks each expected breakage against the diff, and
writes ``acceptance/report.md`` and ``acceptance/report.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packaging.utils import canonicalize_name

from bumpcheck.apidiff import ApiChange, ApiDiffResult, diff_versions

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAIRS = ROOT / "acceptance" / "pairs.json"
REPORT_MD = ROOT / "acceptance" / "report.md"
REPORT_JSON = ROOT / "acceptance" / "report.json"

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_TOP_CHANGES = 3


class PairsError(ValueError):
    """Raised when the pairs file is missing or malformed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PairsError(message)


def _validate_expect(item: object, pair_index: int, index: int) -> dict:
    where = f"pair #{pair_index} expectation #{index}"
    _require(isinstance(item, dict), f"{where} must be an object")
    _require("symbol" in item, f"{where} is missing 'symbol'")
    _require(isinstance(item["symbol"], str), f"{where} field 'symbol' must be a string")
    kind = item.get("kind")
    parameter = item.get("parameter")
    _require(
        kind is None or isinstance(kind, str), f"{where} field 'kind' must be a string or null"
    )
    _require(
        parameter is None or isinstance(parameter, str),
        f"{where} field 'parameter' must be a string or null",
    )
    return {"symbol": item["symbol"], "kind": kind, "parameter": parameter}


def _validate_pair(entry: object, index: int) -> dict:
    where = f"pair #{index}"
    _require(isinstance(entry, dict), f"{where} must be an object")
    for key in ("package", "old", "new", "note", "source"):
        _require(key in entry, f"{where} is missing '{key}'")
        _require(isinstance(entry[key], str), f"{where} field '{key}' must be a string")
    _require(entry["package"] != "", f"{where} field 'package' must not be empty")
    expects = entry.get("expect")
    _require(isinstance(expects, list), f"{where} field 'expect' must be a list")
    return {
        "package": entry["package"],
        "old": entry["old"],
        "new": entry["new"],
        "note": entry["note"],
        "source": entry["source"],
        "expect": [_validate_expect(item, index, i) for i, item in enumerate(expects)],
    }


def load_pairs(path: Path) -> list[dict]:
    """Load and validate the pairs file, raising PairsError when it is invalid."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise PairsError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PairsError(f"{path} is not valid JSON: {exc}") from exc
    _require(isinstance(data, list), f"{path} must contain a JSON array")
    return [_validate_pair(entry, index) for index, entry in enumerate(data)]


def _matches(change: ApiChange, expect: dict) -> bool:
    if change.symbol != expect["symbol"]:
        return False
    if expect["kind"] is not None and change.kind != expect["kind"]:
        return False
    if expect["parameter"] is not None and change.parameter != expect["parameter"]:
        return False
    return True


def _expectation_result(result: ApiDiffResult, expect: dict) -> str:
    if result.status != "ok":
        return "NO_API_DIFF"
    return "HIT" if any(_matches(change, expect) for change in result.changes) else "MISS"


def _top_changes(result: ApiDiffResult) -> list[dict]:
    ranked = sorted(
        result.changes,
        key=lambda change: (
            _SEVERITY_RANK.get(change.severity, 99),
            change.symbol,
            change.kind,
            change.parameter or "",
        ),
    )
    return [
        {
            "kind": change.kind,
            "symbol": change.symbol,
            "severity": change.severity,
            "parameter": change.parameter,
        }
        for change in ranked[:_TOP_CHANGES]
    ]


def _evaluate_pair(pair: dict, cache_dir: Path | None) -> dict:
    base = {
        "package": pair["package"],
        "old": pair["old"],
        "new": pair["new"],
        "note": pair["note"],
        "source": pair["source"],
    }
    try:
        result = diff_versions(pair["package"], pair["old"], pair["new"], cache_dir=cache_dir)
    except Exception as exc:  # noqa: BLE001 - one bad pair must not stop the run
        return {
            **base,
            "status": "error",
            "reason": None,
            "error": f"{type(exc).__name__}: {exc}",
            "change_count": 0,
            "expectations": [{**expect, "result": "ERROR"} for expect in pair["expect"]],
            "top_changes": [],
        }
    return {
        **base,
        "status": "no_api_diff" if result.status == "no_api_diff" else "ok",
        "reason": result.reason,
        "error": None,
        "change_count": len(result.changes),
        "expectations": [
            {**expect, "result": _expectation_result(result, expect)} for expect in pair["expect"]
        ],
        "top_changes": _top_changes(result),
    }


def evaluate(pairs: list[dict], cache_dir: Path | None, only: str | None = None) -> dict:
    """Evaluate every pair and build the report structure."""
    if only is not None:
        wanted = canonicalize_name(only)
        pairs = [pair for pair in pairs if canonicalize_name(pair["package"]) == wanted]

    evaluated = [_evaluate_pair(pair, cache_dir) for pair in pairs]
    summary = {
        "pairs": len(evaluated),
        "expectations": sum(len(pair["expectations"]) for pair in evaluated),
        "hits": 0,
        "misses": 0,
        "errors": 0,
        "no_api_diff": {},
    }
    for pair in evaluated:
        if pair["status"] == "error":
            summary["errors"] += 1
        elif pair["status"] == "no_api_diff":
            reason = pair["reason"] or "unknown"
            summary["no_api_diff"][reason] = summary["no_api_diff"].get(reason, 0) + 1
        for expectation in pair["expectations"]:
            if expectation["result"] == "HIT":
                summary["hits"] += 1
            elif expectation["result"] == "MISS":
                summary["misses"] += 1
    return {"summary": summary, "pairs": evaluated}


def render_markdown(report: dict) -> str:
    """Render the report as Markdown."""
    summary = report["summary"]
    lines = ["# BumpCheck API-diff acceptance", "", "## Summary", ""]
    lines.append(f"- pairs: {summary['pairs']}")
    lines.append(f"- expectations: {summary['expectations']}")
    lines.append(f"- hits: {summary['hits']}")
    lines.append(f"- misses: {summary['misses']}")
    lines.append(f"- errors: {summary['errors']}")
    if summary["no_api_diff"]:
        rendered = ", ".join(
            f"{reason}={count}" for reason, count in sorted(summary["no_api_diff"].items())
        )
        lines.append(f"- no_api_diff: {rendered}")
    else:
        lines.append("- no_api_diff: none")
    lines.append("")

    for pair in report["pairs"]:
        lines.append(f"## {pair['package']} {pair['old']} -> {pair['new']}")
        lines.append("")
        lines.append(f"- status: {pair['status']}")
        if pair["reason"]:
            lines.append(f"- reason: {pair['reason']}")
        if pair["error"]:
            lines.append(f"- error: {pair['error']}")
        if pair["source"]:
            lines.append(f"- source: {pair['source']}")
        if pair["note"]:
            lines.append(f"- note: {pair['note']}")
        lines.append(f"- changes: {pair['change_count']}")
        lines.append("")
        lines.append("| symbol | kind | parameter | result |")
        lines.append("| --- | --- | --- | --- |")
        for expectation in pair["expectations"]:
            lines.append(
                "| {symbol} | {kind} | {parameter} | {result} |".format(
                    symbol=expectation["symbol"],
                    kind=expectation["kind"] or "",
                    parameter=expectation["parameter"] or "",
                    result=expectation["result"],
                )
            )
        lines.append("")
        if pair["top_changes"]:
            lines.append("Top changes:")
            lines.append("")
            for change in pair["top_changes"]:
                parameter = f" (parameter={change['parameter']})" if change["parameter"] else ""
                lines.append(
                    f"- {change['severity']} {change['kind']} {change['symbol']}{parameter}"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run(
    pairs_path: Path = DEFAULT_PAIRS,
    cache_dir: Path | None = None,
    only: str | None = None,
    report_md: Path = REPORT_MD,
    report_json: Path = REPORT_JSON,
) -> dict:
    """Run every pair, write both reports, and return the report structure."""
    report = evaluate(load_pairs(pairs_path), cache_dir, only)
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report_md.write_text(render_markdown(report), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run the acceptance pairs and write reports")
    run_parser.add_argument(
        "--pairs", type=Path, default=DEFAULT_PAIRS, help="path to the pairs JSON file"
    )
    run_parser.add_argument("--cache-dir", type=Path, default=None, help="wheel cache directory")
    run_parser.add_argument("--only", default=None, help="run only this package")
    args = parser.parse_args(argv)

    try:
        report = run(args.pairs, args.cache_dir, args.only)
    except PairsError as exc:
        print(f"invalid pairs file: {exc}", file=sys.stderr)
        return 2

    summary = report["summary"]
    print(
        f"acceptance: {summary['pairs']} pair(s), {summary['expectations']} expectation(s), "
        f"{summary['hits']} hit(s), {summary['misses']} miss(es), "
        f"{summary['errors']} error(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
