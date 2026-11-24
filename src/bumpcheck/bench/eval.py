"""Offline evaluation of labeled benchmark cases."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bumpcheck.bench.labels import Label, load_candidates, load_labels

Split = str
Runner = Callable[[dict[str, Any], float], dict[str, Any]]
RESULTS_SCHEMA_VERSION = 4

_BREAK_CATEGORY_RULES: dict[str, frozenset[str]] = {
    "api_break": frozenset({"R4", "R5"}),
    "python_version_support": frozenset({"R8"}),
    "dependency_conflict": frozenset({"R10"}),
    "behavioral": frozenset({"R4", "R5", "R10"}),
    "other": frozenset(),
}
_SAFE_RULES = frozenset({"R2", "R7"})


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_output(args: list[str], root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def current_fingerprint(root: Path | None = None) -> dict[str, Any]:
    project = Path(root) if root is not None else _project_root()
    git_sha = _git_output(["rev-parse", "HEAD"], project) or "unknown"
    dirty = bool(_git_output(["status", "--porcelain", "--untracked-files=no"], project))
    source_root = project / "src" / "bumpcheck"
    digest = hashlib.sha256()
    count = 0
    if source_root.is_dir():
        paths = sorted(path for path in source_root.rglob("*.py") if "bench" not in path.parts)
        for path in paths:
            relative = path.relative_to(source_root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
            count += 1
    source_sha = digest.hexdigest()
    identity = hashlib.sha256(f"{git_sha}|{dirty}|{source_sha}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": identity,
        "git_sha": git_sha,
        "dirty": dirty,
        "source_sha256": source_sha,
        "source_files": count,
    }


def load_results(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("case_id"), str):
            records.append(payload)
    return records


def write_results(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        for record in sorted(records, key=lambda item: str(item.get("case_id", ""))):
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def _label_records(labels: Mapping[tuple[str, int], Label]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Label]] = {}
    for label in labels.values():
        grouped.setdefault(label.case_id, []).append(label)
    results: dict[str, dict[str, Any]] = {}
    for case_id, group in grouped.items():
        values = {(item.label, item.break_category) for item in group}
        if len(values) > 1:
            results[case_id] = {
                "case_id": case_id,
                "label": "DISPUTED",
                "break_category": None,
                "disputed": True,
                "rounds": sorted((item.round, item.label, item.break_category) for item in group),
            }
            continue
        item = min(group, key=lambda label: label.round)
        if item.label == "EXCLUDE":
            continue
        results[case_id] = {
            "case_id": case_id,
            "label": item.label,
            "break_category": item.break_category,
            "disputed": False,
            "rounds": sorted((label.round, label.label, label.break_category) for label in group),
        }
    return results


def _split_matches(candidate_split: str, requested: str) -> bool:
    if requested == "all":
        return True
    return candidate_split == requested


def _kill_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def run_worker(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    project = _project_root()
    env = os.environ.copy()
    source = str(project / "src")
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("GITHUB_TOKEN", None)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, "-m", "bumpcheck.bench.eval_worker", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=project,
        env=env,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        stdout, stderr = process.communicate(encoded, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process(process)
        return {"error_kind": "timeout", "verdicts": {}, "parse_failures": 0}
    if process.returncode != 0:
        detail = stderr.strip().splitlines()
        message = detail[-1] if detail else f"worker exited {process.returncode}"
        return {
            "error_kind": "worker_crash",
            "worker_error": message[:500],
            "verdicts": {},
            "parse_failures": 0,
        }
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return {"error_kind": "worker_crash", "verdicts": {}, "parse_failures": 0}
    try:
        result = json.loads(lines[-1])
    except ValueError:
        return {"error_kind": "worker_crash", "verdicts": {}, "parse_failures": 0}
    return result if isinstance(result, dict) else {"error_kind": "worker_crash", "verdicts": {}}


def _empty_diagnostics(package: str | None = None) -> dict[str, Any]:
    return {
        "requires_python": {
            "package": package,
            "old": None,
            "new": None,
            "state": "unavailable",
            "project": {
                "source": "unknown",
                "status": "unknown",
                "versions": [],
                "declared_spec": None,
                "warnings": [],
            },
            "assessment": {
                "decided": False,
                "tightened": False,
                "excluded": [],
                "warnings": [],
            },
        },
        "snapshot": {"status": None, "repo_present": False},
        "bumps": [],
    }


def correctness_flags(record: Mapping[str, Any]) -> tuple[bool | None, bool | None]:
    """Return stored verdict/reason correctness flags, deriving them for older records."""
    flags: dict[str, Any] = {}
    stored = record.get("correctness")
    if isinstance(stored, dict):
        flags.update(stored)
    for key in ("verdict_correct", "reason_correct"):
        value = record.get(key)
        if isinstance(value, bool):
            flags[key] = value
    if all(isinstance(flags.get(key), bool) for key in ("verdict_correct", "reason_correct")):
        return flags["verdict_correct"], flags["reason_correct"]

    label = record.get("label")
    if label not in {"BROKE", "OK"}:
        return None, None
    verdicts = record.get("verdicts")
    verdict = verdicts.get("bumpcheck") if isinstance(verdicts, dict) else None
    fired = record.get("fired_rule_ids")
    rules = set(fired.get("bumpcheck", ())) if isinstance(fired, dict) else set()
    if label == "BROKE":
        verdict_correct = verdict in {"REVIEW", "LIKELY_BREAKING"}
        expected = _BREAK_CATEGORY_RULES.get(str(record.get("break_category") or ""), frozenset())
        reason_correct = verdict_correct and bool(rules & expected)
    else:
        verdict_correct = verdict == "SAFE"
        reason_correct = verdict_correct and bool(rules & _SAFE_RULES)
    return verdict_correct, reason_correct


def _correctness_record(record: Mapping[str, Any]) -> dict[str, Any]:
    verdict_correct, reason_correct = correctness_flags(record)
    return {"verdict_correct": verdict_correct, "reason_correct": reason_correct}


def _unknown_result(error_kind: str, package: str | None = None) -> dict[str, Any]:
    return {
        "verdicts": {
            "bumpcheck": "UNKNOWN",
            "always_safe": "UNKNOWN",
            "semver_rule": "UNKNOWN",
        },
        "fired_rule_ids": {},
        "evidence_refs": [],
        "parse_failures": 0,
        "runtime_s": 0.0,
        "error_kind": error_kind,
        "snapshot_status": None,
        "diagnostics": _empty_diagnostics(package),
    }


def evaluate(
    directory: Path,
    *,
    split: str = "dev",
    timeout: float = 120.0,
    force: bool = False,
    case_ids: set[str] | None = None,
    fingerprint: dict[str, Any] | None = None,
    runner: Runner | None = None,
    jobs: int = 1,
) -> list[dict[str, Any]]:
    base = Path(directory)
    candidates = load_candidates(base / "candidates.jsonl")
    labels = load_labels(base / "labels.jsonl")
    truths = _label_records(labels)
    current = fingerprint if fingerprint is not None else current_fingerprint()
    eval_dir = base / "eval"
    results_path = eval_dir / "results.jsonl"
    existing = load_results(results_path)
    by_case = {str(record["case_id"]): record for record in existing}
    if split == "test":
        eval_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fingerprint_id = str(current.get("id") or "unknown")
        with (eval_dir / "test_runs.log").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{stamp} split=test fingerprint={fingerprint_id}\n")

    pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for case_id in sorted(candidates):
        candidate = candidates[case_id]
        requested = str(candidate.get("split") or "dev")
        if not _split_matches(requested, split):
            continue
        if case_ids and case_id not in case_ids:
            continue
        truth = truths.get(case_id)
        if truth is None:
            continue
        previous = by_case.get(case_id)
        if (
            not force
            and previous is not None
            and previous.get("fingerprint") == current
            and previous.get("results_schema_version") == RESULTS_SCHEMA_VERSION
        ):
            continue
        pending.append((candidate, truth))

    snapshot_root = base / "snapshots"
    lock = threading.Lock()

    def one(candidate: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
        case_id = str(candidate["case_id"])
        package = str(candidate.get("package") or "").strip() or None
        record: dict[str, Any] = {
            "case_id": case_id,
            "label": truth["label"],
            "break_category": truth["break_category"],
            "split": candidate.get("split"),
            "sampled_by": candidate.get("sampled_by"),
            "fingerprint": current,
            "results_schema_version": RESULTS_SCHEMA_VERSION,
            "diagnostics": _empty_diagnostics(package),
        }
        if truth.get("disputed"):
            record.update(_unknown_result("disputed_label", package))
            record["rounds"] = truth["rounds"]
            record.update(_correctness_record(record))
            return record
        payload = {
            "case_id": case_id,
            "candidate": candidate,
            "label": truth["label"],
            "break_category": truth["break_category"],
            "snapshot": str(snapshot_root / case_slug(candidate)),
        }
        active_runner = runner or run_worker
        output = active_runner(payload, timeout)
        if not isinstance(output, dict):
            output = _unknown_result("worker_crash", package)
        output.setdefault("verdicts", {})
        output.setdefault("fired_rule_ids", {})
        output.setdefault("evidence_refs", [])
        output.setdefault("parse_failures", 0)
        output.setdefault("runtime_s", 0.0)
        output.setdefault("error_kind", None)
        output.setdefault("snapshot_status", None)
        output.setdefault("diagnostics", _empty_diagnostics(package))
        record.update(output)
        record.update(_correctness_record(record))
        return record

    def store(record: dict[str, Any]) -> None:
        with lock:
            case_id = str(record["case_id"])
            by_case[case_id] = record
            write_results(results_path, list(by_case.values()))

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(one, candidate, truth) for candidate, truth in pending]
        for future in futures:
            store(future.result())

    return [by_case[key] for key in sorted(by_case)]


def case_slug(candidate: Mapping[str, Any]) -> str:
    from bumpcheck.bench.snapshot import case_slug as snapshot_slug

    return snapshot_slug(dict(candidate))
