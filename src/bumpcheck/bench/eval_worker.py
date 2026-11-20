"""Single-case benchmark worker. Reads JSON on stdin and writes JSON on stdout."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from bumpcheck.apidiff import diff_versions
from bumpcheck.baselines import always_safe, semver_rule
from bumpcheck.bench.snapshot import load_manifest
from bumpcheck.check import check as run_check
from bumpcheck.models import Bump
from bumpcheck.usage import trace_usage


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


def _empty_result(error_kind: str, baselines: dict[str, str]) -> dict[str, Any]:
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
    }


def run_case(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else {}
    baselines = _baseline_verdicts(candidate)
    root = Path(str(payload.get("snapshot") or ""))
    manifest = load_manifest(root / "manifest.json")
    if manifest is None:
        return _empty_result("missing_snapshot", baselines)
    status = str(manifest.get("status") or "failed")
    if status != "complete":
        result = _empty_result(status, baselines)
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

        def usage_fn(repo_path: Path, package: str, symbols: Any, **kwargs: Any):
            result = trace_usage(repo_path, package, symbols, **kwargs)
            parse_failures["count"] += sum(
                1 for item in result.files_skipped if item.reason == "unparsable"
            )
            return result

        def diff_fn(name: str, old_version: str, new_version: str, **kwargs: Any):
            return diff_versions(name, old_version, new_version, fetcher=fetcher, **kwargs)

        with tempfile.TemporaryDirectory(prefix="bumpcheck-eval-") as temp:
            result = run_check(
                root / "repo",
                old_text,
                new_text,
                str(candidate.get("lockfile_kind") or "uv.lock"),
                diff_fn=diff_fn,
                usage_fn=usage_fn,
                cache_dir=Path(temp),
            )
        if not result.bumps:
            verdict = "UNKNOWN"
            error_kind: str | None = "no_comparable_bumps"
        else:
            verdict = result.overall
            error_kind = None
        fired = sorted({bump.rule_id for bump in result.bumps})
        evidence = [
            {
                "kind": item.kind,
                "summary": item.summary,
                "file": item.file,
                "line": item.line,
                "symbol": item.symbol,
            }
            for bump in result.bumps
            for item in bump.evidence
        ]
        return {
            "verdicts": {"bumpcheck": verdict, **baselines},
            "fired_rule_ids": {"bumpcheck": fired},
            "evidence_refs": evidence,
            "parse_failures": parse_failures["count"],
            "runtime_s": round(time.monotonic() - started, 4),
            "error_kind": error_kind,
            "snapshot_status": status,
            "warnings": list(result.warnings),
        }
    except Exception as exc:
        result = _empty_result(type(exc).__name__, baselines)
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
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
