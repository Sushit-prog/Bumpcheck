"""Single-case benchmark worker. Reads JSON on stdin and writes JSON on stdout."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from bumpcheck.apidiff import ApiChange, ApiDiffResult, diff_versions
from bumpcheck.baselines import always_safe, semver_rule
from bumpcheck.bench.snapshot import load_manifest
from bumpcheck.check import check as run_check
from bumpcheck.models import Bump
from bumpcheck.projectpython import resolve_project_python
from bumpcheck.pycompat import assess_requires_python_change
from bumpcheck.usage import UsageResult, trace_usage


def _normalize_metadata_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 4 or segments[-1] != "json":
        return url
    name = canonicalize_name(urllib.parse.unquote(segments[-3]))
    version = urllib.parse.unquote(segments[-2])
    return f"{parts.scheme}://{parts.netloc}/pypi/{name}/{version}/json"


def _offline_fetcher(manifest: dict[str, Any], root: Path):
    metadata: dict[str, bytes] = {}
    wheels: dict[str, bytes] = {}
    for label in ("old", "new"):
        entry = manifest.get(label)
        if not isinstance(entry, dict):
            continue
        metadata_file = entry.get("metadata_file")
        wheel_file = entry.get("wheel_file")
        if isinstance(metadata_file, str):
            metadata[_normalize_metadata_url(str(entry.get("metadata_url") or ""))] = (
                root / metadata_file
            ).read_bytes()
        if isinstance(wheel_file, str):
            wheel_url = str(entry.get("wheel_url") or "")
            wheels[wheel_url] = (root / wheel_file).read_bytes()
            wheels[wheel_url.lower()] = (root / wheel_file).read_bytes()

    def fetch(url: str) -> bytes:
        normalized = _normalize_metadata_url(url)
        if normalized in metadata:
            return metadata[normalized]
        if url in wheels:
            return wheels[url]
        if url.lower() in wheels:
            return wheels[url.lower()]
        raise RuntimeError(f"unexpected offline URL: {url}")

    return fetch


def _baseline_verdicts(candidate: dict[str, Any]) -> dict[str, str]:
    try:
        bump = Bump(
            package=str(candidate.get("package") or ""),
            old_version=candidate.get("old_version"),
            new_version=candidate.get("new_version"),
            source=str(candidate.get("lockfile_kind") or "uv.lock"),
        )
    except Exception:
        return {"always_safe": "UNKNOWN", "semver_rule": "UNKNOWN"}
    return {"always_safe": always_safe(bump), "semver_rule": semver_rule(bump)}


DiagnosticPair = tuple[ApiDiffResult | None, UsageResult | None]


def _empty_diagnostics(package: str | None = None) -> dict[str, Any]:
    return {
        "requires_python": {"package": package, "old": None, "new": None},
        "snapshot": {"status": None, "repo_present": False},
        "bumps": [],
    }


def _snapshot_diagnostics(manifest: dict[str, Any] | None, root: Path) -> dict[str, Any]:
    return {
        "status": str(manifest.get("status") or "failed") if manifest is not None else None,
        "repo_present": (root / "repo").is_dir(),
    }


def _snapshot_requires_python(
    manifest: dict[str, Any] | None,
    root: Path,
    package: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"package": package, "old": None, "new": None}
    if manifest is None:
        return result
    for side in ("old", "new"):
        entry = manifest.get(side)
        if not isinstance(entry, dict):
            continue
        metadata_file = entry.get("metadata_file")
        if not isinstance(metadata_file, str) or not metadata_file:
            continue
        try:
            payload = json.loads((root / metadata_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        value = info.get("requires_python")
        if isinstance(value, str) and value.strip():
            result[side] = value.strip()
    return result


def _requires_python_diagnostics(
    manifest: dict[str, Any] | None,
    root: Path,
    package: str | None,
    api: ApiDiffResult | None = None,
) -> dict[str, Any]:
    snapshot = _snapshot_requires_python(manifest, root, package)
    old = snapshot["old"]
    new = snapshot["new"]
    state = "available" if manifest is not None else "unavailable"
    if api is not None:
        if api.old_requires_python is not None:
            old = api.old_requires_python
        if api.new_requires_python is not None:
            new = api.new_requires_python
        if (
            api.old_requires_python_state == "unavailable"
            or api.new_requires_python_state == "unavailable"
        ):
            state = "unavailable"
    project = resolve_project_python(root / "repo")
    assessment = assess_requires_python_change(old, new, project.versions, state=state)
    return {
        "package": package,
        "old": old,
        "new": new,
        "state": state,
        "project": {
            "source": project.source,
            "status": project.status,
            "versions": list(project.versions),
            "declared_spec": project.declared_spec,
            "warnings": list(project.warnings),
        },
        "assessment": {
            "decided": assessment.decided,
            "tightened": assessment.tightened,
            "excluded": list(assessment.excluded),
            "warnings": list(assessment.warnings),
        },
    }


def _api_diagnostics(verdict: Any, api: ApiDiffResult | None) -> dict[str, Any]:
    changes = list(api.changes) if api is not None else []
    severity_counts = {name: 0 for name in ("high", "medium", "low")}
    severity_counts.update(Counter(str(change.severity) for change in changes))
    return {
        "status": str(api.status) if api is not None else (verdict.api_status or "not_run"),
        "reason": str(api.reason) if api is not None and api.reason is not None else None,
        "change_count": len(changes),
        "changes_by_kind": dict(sorted(Counter(str(change.kind) for change in changes).items())),
        "changes_by_severity": severity_counts,
    }


def _usage_diagnostics(usage: UsageResult | None) -> dict[str, Any]:
    if usage is None:
        return {
            "status": "not_run",
            "files_scanned": 0,
            "files_skipped": 0,
            "hit_count": 0,
            "risk_flags": [],
        }
    return {
        "status": "ok",
        "files_scanned": usage.files_scanned,
        "files_skipped": len(usage.files_skipped),
        "hit_count": len(usage.hits),
        "risk_flags": sorted(set(usage.risks)),
    }


def _change_labels(changes: list[ApiChange]) -> dict[str, str]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for change in changes:
        symbol = str(change.symbol)
        kind = str(change.kind)
        bucket = grouped.setdefault(symbol, {}).setdefault(kind, {"params": [], "plain": False})
        if change.parameter:
            if change.parameter not in bucket["params"]:
                bucket["params"].append(change.parameter)
        else:
            bucket["plain"] = True
    labels: dict[str, str] = {}
    for symbol, kinds in grouped.items():
        parts: list[str] = []
        for kind, data in kinds.items():
            if data["params"]:
                parts.append(f"{kind}: {', '.join(data['params'])}")
            else:
                parts.append(kind)
        labels[symbol] = "; ".join(parts)
    return labels


def _build_evidence_refs(
    bumps: tuple[Any, ...],
    hook_data: dict[int, DiagnosticPair],
) -> list[dict[str, Any]]:
    labels: dict[str, str] = {}
    for api, _usage in hook_data.values():
        if api is None:
            continue
        for symbol, label in _change_labels(api.changes).items():
            labels.setdefault(symbol, label)
    refs: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for bump in bumps:
        for item in bump.evidence:
            ref: dict[str, Any] = {
                "kind": item.kind,
                "summary": item.summary,
                "file": item.file,
                "line": item.line,
                "symbol": item.symbol,
            }
            symbol = item.symbol
            if (
                item.kind == "reason"
                and isinstance(symbol, str)
                and item.summary == f"No usage found for {symbol}"
                and symbol in labels
            ):
                ref["summary"] = f"No usage found for {symbol} ({labels[symbol]})"
                key: tuple[Any, ...] = ("no_usage", symbol)
            else:
                key = (
                    "full",
                    ref["kind"],
                    ref["summary"],
                    ref["file"],
                    ref["line"],
                    ref["symbol"],
                )
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
    return refs


def _empty_result(
    error_kind: str,
    baselines: dict[str, str],
    *,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "verdicts": {
            "bumpcheck": "UNKNOWN",
            **baselines,
        },
        "fired_rule_ids": {},
        "evidence_refs": [],
        "parse_failures": 0,
        "runtime_s": 0.0,
        "error_kind": error_kind,
        "snapshot_status": None,
        "diagnostics": diagnostics if diagnostics is not None else _empty_diagnostics(),
    }


def run_case(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    baselines = _baseline_verdicts(candidate)
    root = Path(str(payload.get("snapshot") or ""))
    package_value = str(candidate.get("package") or "").strip()
    package = package_value or None
    manifest = load_manifest(root / "manifest.json")
    if manifest is None:
        diagnostics = _empty_diagnostics(package)
        diagnostics["snapshot"] = _snapshot_diagnostics(None, root)
        return _empty_result("missing_snapshot", baselines, diagnostics=diagnostics)
    status = str(manifest.get("status") or "failed")
    if status != "complete":
        diagnostics = _empty_diagnostics(package)
        diagnostics["snapshot"] = _snapshot_diagnostics(manifest, root)
        result = _empty_result(status, baselines, diagnostics=diagnostics)
        result["snapshot_status"] = status
        return result

    try:
        lockfiles = manifest.get("lockfiles") if isinstance(manifest.get("lockfiles"), dict) else {}
        paths = [str(path) for path in (candidate.get("lockfile_paths") or [])] or list(lockfiles)
        if not paths:
            raise ValueError("no lockfile path recorded")
        chosen = next((path for path in paths if path in lockfiles), None)
        if chosen is None:
            raise ValueError("lockfile was not snapshotted")
        pair = lockfiles[chosen]
        old_text = (root / pair["old"]).read_text(encoding="utf-8")
        new_text = (root / pair["new"]).read_text(encoding="utf-8")
        fetcher = _offline_fetcher(manifest, root)
        parse_failures = {"count": 0}
        hook_data: dict[int, DiagnosticPair] = {}

        def usage_fn(repo_path: Path, package: str, symbols: Any, **kwargs: Any):
            usage_result = trace_usage(repo_path, package, symbols, **kwargs)
            parse_failures["count"] += sum(
                1 for item in usage_result.files_skipped if item.reason == "unparsable"
            )
            return usage_result

        def diff_fn(name: str, old_version: str, new_version: str, **kwargs: Any):
            return diff_versions(name, old_version, new_version, fetcher=fetcher, **kwargs)

        def on_bump(verdict: Any, api: ApiDiffResult | None, usage: UsageResult | None) -> None:
            hook_data[id(verdict)] = (api, usage)

        with tempfile.TemporaryDirectory(prefix="bumpcheck-eval-") as temp:
            check_result = run_check(
                root / "repo",
                old_text,
                new_text,
                str(candidate.get("lockfile_kind") or "uv.lock"),
                diff_fn=diff_fn,
                usage_fn=usage_fn,
                cache_dir=Path(temp),
                on_bump=on_bump,
            )
        if not check_result.bumps:
            verdict = "UNKNOWN"
            error_kind: str | None = "no_comparable_bumps"
        else:
            verdict = check_result.overall
            error_kind = None
        fired = sorted({bump.rule_id for bump in check_result.bumps})
        evidence = _build_evidence_refs(check_result.bumps, hook_data)
        first_api = next(
            (api for api, _ in hook_data.values() if api is not None),
            None,
        )
        diagnostics: dict[str, Any] = {
            "requires_python": _requires_python_diagnostics(manifest, root, package, first_api),
            "snapshot": _snapshot_diagnostics(manifest, root),
            "bumps": [],
        }
        for bump in check_result.bumps:
            api, usage = hook_data.get(id(bump), (None, None))
            diagnostics["bumps"].append(
                {
                    "package": bump.package,
                    "old_version": bump.old_version,
                    "new_version": bump.new_version,
                    "rule_id": bump.rule_id,
                    "api_diff": _api_diagnostics(bump, api),
                    "usage": _usage_diagnostics(usage),
                }
            )
        return {
            "verdicts": {"bumpcheck": verdict, **baselines},
            "fired_rule_ids": {"bumpcheck": fired},
            "evidence_refs": evidence,
            "parse_failures": parse_failures["count"],
            "runtime_s": round(time.monotonic() - started, 4),
            "error_kind": error_kind,
            "snapshot_status": status,
            "warnings": list(check_result.warnings),
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        diagnostics = _empty_diagnostics(package)
        diagnostics["snapshot"] = _snapshot_diagnostics(manifest, root)
        result = _empty_result(type(exc).__name__, baselines, diagnostics=diagnostics)
        result["snapshot_status"] = status
        result["runtime_s"] = round(time.monotonic() - started, 4)
        result["worker_error"] = str(exc)[:500]
        return result


def main() -> None:
    source = sys.stdin.read()
    try:
        payload = json.loads(source)
        if not isinstance(payload, dict):
            raise ValueError("worker input must be a JSON object")
        result = run_case(payload)
    except Exception as exc:
        result = {
            "verdicts": {
                "bumpcheck": "UNKNOWN",
                "always_safe": "UNKNOWN",
                "semver_rule": "UNKNOWN",
            },
            "fired_rule_ids": {},
            "evidence_refs": [],
            "parse_failures": 0,
            "runtime_s": 0.0,
            "error_kind": "worker_crash",
            "worker_error": f"{type(exc).__name__}: {exc}"[:500],
            "snapshot_status": None,
            "diagnostics": _empty_diagnostics(),
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
