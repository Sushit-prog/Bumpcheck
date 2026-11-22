"""Read-only inspection helper for one mined benchmark candidate."""

from __future__ import annotations

import base64
import difflib
import json
import re
import tomllib
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from bumpcheck.bench import github
from bumpcheck.bench.github import (
    GitHubClient,
    GitHubError,
    GitHubFatalError,
    HttpStatusError,
)
from bumpcheck.bench.labels import load_candidates
from bumpcheck.bench.mine import FAILURE_CONCLUSIONS
from bumpcheck.lockdiff import SUPPORTED_KINDS, LockParseError, extract_pinned_versions
from bumpcheck.projectpython import (
    VersionScalar,
    _composed_jobs,
    _format_version_scalar,
    _job_matrix_node,
    _matrix_values,
    _python_version_values,
    _scalar_text,
    _unquoted_float_warning,
)
from bumpcheck.pycompat import (
    ConcretePythonVersion,
    NonConcretePythonVersion,
    allows_python,
    interpret_python_version,
    python_floor,
)

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/{version}/json"
WORKFLOW_DIR = ".github/workflows"
_WORKFLOW_SUFFIXES = (".yml", ".yaml")
_RUN_TOOL_RE = re.compile(
    r"\b(?:pip\d*|uv|poetry|pytest|pylint|ruff|mypy|tox|nox)\b", re.IGNORECASE
)
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^\s]*")
_CREDENTIAL_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")
_CHECK_TOKEN_RE = re.compile(r"[^()\[\]\s]+")
_VERSION_LIKE_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.(?:\d+|[xX*])){1,2}(?:t|-dev)?)(?![\d.])",
    re.IGNORECASE,
)
_STRICT_PYTHON_TOKEN_RE = re.compile(
    r"(?:3\.(?:[0-9]|[1-9][0-9])|2\.7)(?:t|-dev)?\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PypiInfo:
    version: str
    requires_python: str | None
    requires_dist: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowJob:
    job_id: str
    name: str | None
    python_versions: tuple[VersionScalar, ...]
    run_lines: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowFile:
    path: str
    triggers: tuple[str, ...]
    jobs: tuple[WorkflowJob, ...]


@dataclass(frozen=True)
class DependencyChanges:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]


@dataclass(frozen=True)
class CheckLocation:
    path: str
    job_id: str
    version: str | None


PypiCache = dict[tuple[str, str], PypiInfo | None]
MAX_PYPI_LOOKUPS = 40
_BASE_NO_FINDING = (
    "base already incompatible: none found (hint only; does not show the base was green)"
)


def redact(text: str) -> str:
    text = _CREDENTIAL_RE.sub(r"\1***@", text)
    return _URL_QUERY_RE.sub(r"\1", text)


def find_candidate(
    case_id: str, directory: Path, also_dirs: list[Path] | None = None
) -> tuple[dict[str, Any], Path]:
    """Find a candidate in ``directory``, falling back to each ``also_dir``."""
    seen: set[Path] = set()
    for candidate_dir in (directory, *(also_dirs or ())):
        try:
            resolved = candidate_dir.resolve()
        except OSError:
            resolved = candidate_dir
        if resolved in seen:
            continue
        seen.add(resolved)
        path = candidate_dir / "candidates.jsonl"
        if not path.is_file():
            continue
        record = load_candidates(path).get(case_id)
        if record is not None:
            return record, path
    raise ValueError(f"candidate not found: {case_id}")


def _resolve_repos(client: GitHubClient, candidate: dict[str, Any]) -> tuple[str, str]:
    repo = str(candidate.get("repo") or "")
    number = candidate.get("pr_number")
    if not repo or not isinstance(number, int) or isinstance(number, bool):
        return repo, repo
    try:
        payload = client.get_json(f"{github.API_ROOT}/repos/{repo}/pulls/{number}")
    except GitHubFatalError:
        raise
    except (GitHubError, OSError, ValueError):
        return repo, repo
    if not isinstance(payload, dict):
        return repo, repo
    base_repo = repo
    head_repo = repo
    base = payload.get("base")
    head = payload.get("head")
    if isinstance(base, dict) and isinstance(base.get("full_name"), str):
        base_repo = base["full_name"]
    if isinstance(head, dict) and isinstance(head.get("full_name"), str):
        head_repo = head["full_name"]
    return base_repo, head_repo


def _fetch_raw(client: GitHubClient, repo: str, path: str, sha: str) -> str | None:
    if not repo or not path or not sha:
        return None
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return None
    encoded = urllib.parse.quote(path, safe="/")
    url = f"{github.RAW_ROOT}/{owner}/{name}/{sha}/{encoded}"
    try:
        body = client.get_bytes(url)
    except HttpStatusError as exc:
        if exc.status == 404:
            return None
        raise
    return body.decode("utf-8-sig", errors="replace")


def _pypi_url(name: str, version: str) -> str:
    return PYPI_JSON_URL.format(
        name=urllib.parse.quote(name, safe=""),
        version=urllib.parse.quote(version, safe=""),
    )


def _fetch_pypi(
    client: GitHubClient,
    name: str,
    version: str | None,
    pypi_fetch: Callable[[str], bytes] | None = None,
    cache: PypiCache | None = None,
) -> PypiInfo | None:
    if not name or not version:
        return None
    key = (canonicalize_name(name), version)
    if cache is not None and key in cache:
        return cache[key]
    url = _pypi_url(name, version)
    fetch = pypi_fetch if pypi_fetch is not None else client.get_external_bytes
    try:
        body = fetch(url)
        payload = json.loads(body.decode("utf-8"))
    except GitHubFatalError:
        raise
    except (GitHubError, OSError, ValueError, UnicodeDecodeError):
        result = None
    else:
        info = payload.get("info") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            result = None
        else:
            requires_python = info.get("requires_python")
            if not isinstance(requires_python, str) or not requires_python.strip():
                requires_python = None
            raw_dist = info.get("requires_dist")
            requires_dist = (
                tuple(item for item in raw_dist if isinstance(item, str))
                if isinstance(raw_dist, list)
                else ()
            )
            fetched_version = info.get("version")
            result = PypiInfo(
                version=fetched_version if isinstance(fetched_version, str) else version,
                requires_python=requires_python,
                requires_dist=requires_dist,
            )
    if cache is not None:
        cache[key] = result
    return result


def _requirement_index(lines: tuple[str, ...]) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for line in lines:
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            normalized = " ".join(line.split())
            name = f"__raw__{normalized}"
        else:
            normalized = str(requirement)
            name = canonicalize_name(requirement.name)
        index.setdefault(name, {})[normalized] = line
    return index


def diff_requires_dist(old_lines: tuple[str, ...], new_lines: tuple[str, ...]) -> DependencyChanges:
    old_index = _requirement_index(old_lines)
    new_index = _requirement_index(new_lines)
    added = tuple(
        new_index[name][normalized]
        for name in sorted(set(new_index) - set(old_index))
        for normalized in sorted(new_index[name])
    )
    removed = tuple(
        old_index[name][normalized]
        for name in sorted(set(old_index) - set(new_index))
        for normalized in sorted(old_index[name])
    )
    changed: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for name in sorted(set(old_index) & set(new_index)):
        old_norms = set(old_index[name])
        new_norms = set(new_index[name])
        if old_norms == new_norms:
            continue
        old_only = tuple(old_index[name][norm] for norm in sorted(old_norms - new_norms))
        new_only = tuple(new_index[name][norm] for norm in sorted(new_norms - old_norms))
        changed.append((name, old_only, new_only))
    return DependencyChanges(added=added, removed=removed, changed=tuple(changed))


def _workflow_triggers(document: dict[str, Any]) -> tuple[str, ...]:
    raw = document.get("on")
    if raw is None and True in document:
        raw = document.get(True)
    if raw is True:
        return ("true",)
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(item) for item in raw)
    if isinstance(raw, dict):
        return tuple(sorted(str(key) for key in raw))
    return (str(raw),)


def _parse_workflow(path: str, text: str) -> WorkflowFile | None:
    try:
        document = yaml.safe_load(text)
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    if not isinstance(document, dict):
        return None
    triggers = _workflow_triggers(document)
    composed_jobs = _composed_jobs(root)
    jobs: list[WorkflowJob] = []
    raw_jobs = document.get("jobs")
    if isinstance(raw_jobs, dict):
        for job_id in sorted(raw_jobs, key=str):
            job = raw_jobs[job_id]
            if not isinstance(job, dict):
                continue
            name = job.get("name")
            job_node = composed_jobs.get(str(job_id))
            versions: tuple[VersionScalar, ...] = ()
            if job_node is not None:
                matrix_node = _job_matrix_node(job_node)
                matrix_values = _matrix_values(matrix_node) if matrix_node is not None else {}
                versions = _python_version_values(job_node, matrix_values)
            runs: list[str] = []
            steps = job.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                        continue
                    for line in step["run"].splitlines():
                        stripped = line.strip()
                        if stripped and _RUN_TOOL_RE.search(stripped):
                            runs.append(stripped)
            jobs.append(
                WorkflowJob(
                    job_id=str(job_id),
                    name=name if isinstance(name, str) else None,
                    python_versions=versions,
                    run_lines=tuple(dict.fromkeys(runs)),
                )
            )
    return WorkflowFile(path=path, triggers=triggers, jobs=tuple(jobs))


def _fetch_workflows(
    client: GitHubClient, repo: str, sha: str
) -> tuple[list[WorkflowFile], list[str], bool]:
    owner, _, name = repo.partition("/")
    if not owner or not name or not sha:
        return [], [], False
    url = (
        f"{github.API_ROOT}/repos/{owner}/{name}/contents/{WORKFLOW_DIR}"
        f"?ref={urllib.parse.quote(sha, safe='')}"
    )
    try:
        payload = client.get_json(url)
    except GitHubFatalError:
        raise
    except (GitHubError, OSError, ValueError):
        return [], [], False
    if not isinstance(payload, list):
        return [], [], False
    entries = [
        entry
        for entry in payload
        if isinstance(entry, dict)
        and str(entry.get("type") or "") == "file"
        and str(entry.get("name") or "").endswith(_WORKFLOW_SUFFIXES)
    ]
    entries.sort(key=lambda entry: str(entry.get("name") or ""))
    files: list[WorkflowFile] = []
    failures: list[str] = []
    for entry in entries:
        file_name = str(entry.get("name") or "")
        content_url = entry.get("url")
        if not isinstance(content_url, str):
            failures.append(file_name)
            continue
        try:
            file_payload = client.get_json(content_url)
        except GitHubFatalError:
            raise
        except (GitHubError, OSError, ValueError):
            failures.append(file_name)
            continue
        if not isinstance(file_payload, dict):
            failures.append(file_name)
            continue
        content = file_payload.get("content")
        if not isinstance(content, str):
            failures.append(file_name)
            continue
        try:
            text = base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            failures.append(file_name)
            continue
        parsed = _parse_workflow(file_name, text)
        if parsed is None:
            failures.append(file_name)
            continue
        files.append(parsed)
    return files, failures, True


def _fetch_requires_python(client: GitHubClient, repo: str, sha: str) -> str | None:
    text = _fetch_raw(client, repo, "pyproject.toml", sha)
    if text is None:
        return None
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    project = document.get("project")
    if not isinstance(project, dict):
        return None
    value = project.get("requires-python")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _package_pattern(package: str) -> re.Pattern[str] | None:
    key = canonicalize_name(package)
    parts = [re.escape(part) for part in key.split("-") if part]
    if not parts:
        return None
    body = r"[-_.]".join(parts)
    return re.compile(rf"(?<![a-z0-9._-]){body}(?![a-z0-9._-])", re.IGNORECASE)


def _filter_diff_for_package(diff_lines: list[str], package: str) -> list[str]:
    pattern = _package_pattern(package)
    if pattern is None:
        return []
    headers: list[str] = []
    hunks: list[str] = []
    current: list[str] | None = None
    has_package_line = False
    for line in diff_lines:
        if line.startswith("---") or line.startswith("+++"):
            headers.append(line)
        elif line.startswith("@@"):
            if current is not None and has_package_line:
                hunks.extend(current)
            current = [line]
            has_package_line = False
        elif current is not None and pattern.search(line[1:] if line[:1] in {"+", "-"} else line):
            current.append(line)
            has_package_line = True
    if current is not None and has_package_line:
        hunks.extend(current)
    return headers + hunks if hunks else []


def _lockfile_diff(
    client: GitHubClient,
    base_repo: str,
    head_repo: str,
    path: str,
    base_sha: str,
    head_sha: str,
    package: str,
) -> list[str] | None:
    old_text = _fetch_raw(client, base_repo, path, base_sha)
    new_text = _fetch_raw(client, head_repo, path, head_sha)
    if old_text is None or new_text is None:
        return None
    diff = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return _filter_diff_for_package(list(diff), package)


def _check_name_tokens(check_name: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for raw in _CHECK_TOKEN_RE.findall(check_name):
        token = raw.strip(".,;:")
        if not token or ":" in token or "/" in token:
            continue
        tokens.append(token)
    return tuple(tokens)


def _check_version_tokens(check_name: str) -> tuple[str, ...]:
    return tuple(match.group(1) for match in _VERSION_LIKE_RE.finditer(check_name))


def _unmatched_check_versions(check_name: str, accepted: str | None) -> tuple[str, ...]:
    return tuple(token for token in _check_version_tokens(check_name) if token != accepted)


def _strict_python_token(token: str) -> ConcretePythonVersion | None:
    if _STRICT_PYTHON_TOKEN_RE.fullmatch(token) is None:
        return None
    result = interpret_python_version(token, source="version")
    return result if isinstance(result, ConcretePythonVersion) else None


def _job_versions(job: WorkflowJob) -> set[tuple[int, int]]:
    versions: set[tuple[int, int]] = set()
    for scalar in job.python_versions:
        result = interpret_python_version(scalar.raw, source=scalar.source)
        if isinstance(result, ConcretePythonVersion):
            versions.add((result.major, result.minor))
    return versions


def infer_check_location(
    workflows: tuple[WorkflowFile, ...] | list[WorkflowFile], check_name: str
) -> CheckLocation | None:
    lowered = check_name.lower()
    strict_tokens: list[tuple[str, ConcretePythonVersion]] = []
    for token in _check_name_tokens(check_name):
        version = _strict_python_token(token)
        if version is not None:
            strict_tokens.append((token, version))
    best: tuple[int, str, str, str | None] | None = None
    for workflow in workflows:
        for job in workflow.jobs:
            score = 0
            job_id = job.job_id.lower()
            if job_id and (job_id == lowered or job_id in lowered):
                score += 2
            name = (job.name or "").lower()
            if name and (name == lowered or name in lowered):
                score += 2
            declared = _job_versions(job)
            matched_version: str | None = None
            for token, version in strict_tokens:
                if (version.major, version.minor) in declared:
                    score += 1
                    matched_version = token
                    break
            if score <= 0:
                continue
            key = (-score, workflow.path, job.job_id)
            if best is None or key < (-best[0], best[1], best[2]):
                best = (score, workflow.path, job.job_id, matched_version)
    if best is None:
        return None
    return CheckLocation(path=best[1], job_id=best[2], version=best[3])


def _header_lines(candidate: dict[str, Any]) -> list[str]:
    repo = str(candidate.get("repo") or "")
    number = candidate.get("pr_number")
    url = (
        f"https://github.com/{repo}/pull/{number}"
        if repo and isinstance(number, int) and not isinstance(number, bool)
        else "unavailable"
    )
    package = str(candidate.get("package") or "unavailable")
    old_version = candidate.get("old_version") or "?"
    new_version = candidate.get("new_version") or "?"
    raw_merged_at = candidate.get("merged_at")
    if isinstance(raw_merged_at, str) and raw_merged_at.strip():
        merged_at = raw_merged_at
    elif candidate.get("merged") is False:
        merged_at = "not merged"
    else:
        merged_at = "unavailable"
    return [
        f"case: {candidate.get('case_id', '')}",
        f"url: {url}",
        f"package: {package} {old_version}->{new_version}",
        f"merged_at: {merged_at}",
    ]


def _pypi_lines(old_info: PypiInfo | None, new_info: PypiInfo | None) -> list[str]:
    old_python = (
        old_info.requires_python if old_info and old_info.requires_python else "unavailable"
    )
    new_python = (
        new_info.requires_python if new_info and new_info.requires_python else "unavailable"
    )
    lines = [
        "pypi:",
        f"  old requires_python: {old_python}",
        f"  new requires_python: {new_python}",
    ]
    if old_info is None or new_info is None:
        lines.append("  dependency changes: unavailable")
        return lines
    changes = diff_requires_dist(old_info.requires_dist, new_info.requires_dist)
    if not (changes.added or changes.removed or changes.changed):
        lines.append("  dependency changes: none")
        return lines
    entries = [f"+ {line}" for line in changes.added]
    entries.extend(f"- {line}" for line in changes.removed)
    for name, old_lines, new_lines in changes.changed:
        old_text = ", ".join(old_lines) or "none"
        new_text = ", ".join(new_lines) or "none"
        entries.append(f"~ {name}: {old_text} -> {new_text}")
    count = len(entries)
    shown = entries[:10]
    more = f" (+{count - 10} more)" if count > 10 else ""
    label = "change" if count == 1 else "changes"
    lines.append(
        f"  dependency changes: {count} {label} ({len(shown)} shown): " + "; ".join(shown) + more
    )
    return lines


def _workflow_lines(
    files: list[WorkflowFile],
    failures: list[str],
    listing_ok: bool,
    *,
    include_runs: bool = True,
) -> list[str]:
    if not listing_ok:
        return ["workflows: unavailable"]
    if not files and not failures:
        return ["workflows: none"]
    lines = ["workflows:"]
    for workflow in files:
        lines.append(f"  {workflow.path}")
        triggers = ", ".join(workflow.triggers) if workflow.triggers else "none"
        lines.append(f"    on: {triggers}")
        lines.append("    jobs:")
        if not workflow.jobs:
            lines.append("      none")
            continue
        for job in workflow.jobs:
            lines.append(f"      {job.job_id}")
            if job.name:
                lines.append(f"        name: {job.name}")
            versions = (
                ", ".join(_format_version_scalar(scalar) for scalar in job.python_versions)
                if job.python_versions
                else "none"
            )
            lines.append(f"        python-version matrix: {versions}")
            raw = (
                ", ".join(_scalar_text(scalar) for scalar in job.python_versions)
                if job.python_versions
                else "none"
            )
            lines.append(f"        raw python-version scalars: {raw}")
            if include_runs:
                lines.append("        run:")
                if job.run_lines:
                    for run in job.run_lines:
                        lines.append(f"          - {run}")
                else:
                    lines.append("          none")
    for name in failures:
        lines.append(f"  {name}: unavailable")
    return lines


def _lockfile_lines(
    client: GitHubClient,
    candidate: dict[str, Any],
    base_repo: str,
    head_repo: str,
    package: str,
    *,
    include_diff: bool = True,
) -> list[str]:
    paths = candidate.get("lockfile_paths")
    if not isinstance(paths, list) or not paths:
        return ["lockfile changes: none"]
    base_sha = str(candidate.get("base_sha") or "")
    head_sha = str(candidate.get("head_sha") or "")
    lines = ["lockfile changes:"]
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        lines.append(f"  {path}:")
        try:
            diff = _lockfile_diff(client, base_repo, head_repo, path, base_sha, head_sha, package)
        except GitHubFatalError:
            raise
        except (GitHubError, OSError, ValueError):
            diff = None
        if diff is None:
            lines.append("    unavailable")
        elif not diff:
            lines.append("    no package lines changed")
        elif include_diff:
            for line in diff:
                lines.append(f"    {line}")
        else:
            lines.append("    diff omitted in batch")
    return lines


def _verdict_lines(
    workflows: list[WorkflowFile],
    old_info: PypiInfo | None,
    new_info: PypiInfo | None,
    old_version_available: bool,
    repo_requires_python: str | None,
    check_versions: dict[Version, str] | None = None,
) -> list[str]:
    versions: dict[Version, str] = {}
    non_concrete: list[str] = []
    for workflow in workflows:
        for job in workflow.jobs:
            for scalar in job.python_versions:
                result = interpret_python_version(scalar.raw, source=scalar.source)
                if isinstance(result, NonConcretePythonVersion):
                    line = (
                        f"  Python {scalar.raw}: non-concrete ({result.reason}); "
                        "requires-python not evaluated"
                    )
                    non_concrete.append(line)
                    continue
                version = result.as_version()
                label = str(version)
                warning = _unquoted_float_warning(scalar)
                if warning is not None:
                    label = f"{version} {warning}"
                versions.setdefault(version, label)
    if repo_requires_python:
        floor = python_floor(repo_requires_python)
        if floor is not None:
            versions.setdefault(floor, str(floor))
    for version in check_versions or {}:
        versions.setdefault(version, str(version))
    if not versions and not non_concrete:
        return ["verdict hints: none"]
    new_spec = new_info.requires_python if new_info else None
    old_spec = old_info.requires_python if old_info else None
    lines = ["verdict hints:"]
    for version, label in sorted(versions.items()):
        new_allowed = allows_python(new_spec, version)
        if new_allowed is None:
            new_state = "unknown (requires_python unavailable)"
        elif new_allowed:
            new_state = "allowed"
        else:
            new_state = f"NOT allowed (requires {new_spec})"
        if not old_version_available:
            old_state = "unavailable"
        else:
            old_allowed = allows_python(old_spec, version)
            if old_allowed is None:
                old_state = "unknown (requires_python unavailable)"
            elif old_allowed:
                old_state = "allowed"
            else:
                old_state = f"NOT allowed (requires {old_spec})"
        lines.append(f"  Python {label}: new version {new_state}; old version {old_state}")
    lines.extend(dict.fromkeys(non_concrete))
    return lines


def _failing_check_lines(candidate: dict[str, Any], workflows: list[WorkflowFile]) -> list[str]:
    checks = candidate.get("ci_checks")
    failing = [
        check
        for check in checks or []
        if isinstance(check, dict) and str(check.get("conclusion") or "") in FAILURE_CONCLUSIONS
    ]
    if not failing:
        return ["failing checks: none"]
    lines = ["failing checks:"]
    failing.sort(key=lambda check: str(check.get("name") or ""))
    for check in failing:
        name = str(check.get("name") or "")
        conclusion = str(check.get("conclusion") or "")
        location = infer_check_location(workflows, name)
        accepted = location.version if location is not None else None
        suffix = f" [{accepted}]" if accepted else ""
        for unmatched in _unmatched_check_versions(name, accepted):
            suffix += f"; unmatched check-name version: {unmatched}"
        if location is None:
            lines.append(f"  {name} ({conclusion}) -> unavailable{suffix}")
            continue
        lines.append(f"  {name} ({conclusion}) -> {location.path} job {location.job_id}{suffix}")
    return lines


def _failing_checks(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    checks = candidate.get("ci_checks")
    return [
        check
        for check in checks or []
        if isinstance(check, dict) and str(check.get("conclusion") or "") in FAILURE_CONCLUSIONS
    ]


def _failing_targets(
    candidate: dict[str, Any], workflows: list[WorkflowFile]
) -> tuple[dict[Version, str], list[str]]:
    concrete: dict[Version, str] = {}
    notices: list[str] = []
    for check in _failing_checks(candidate):
        name = str(check.get("name") or "")
        location = infer_check_location(workflows, name)
        accepted = location.version if location is not None else None
        if accepted is not None:
            result = _strict_python_token(accepted)
            if result is not None:
                concrete.setdefault(result.as_version(), name)
        for unmatched in _unmatched_check_versions(name, accepted):
            notices.append(f"unmatched check-name version: {unmatched} (check {name})")

    matrix: dict[Version, str] = {}
    for workflow in workflows:
        for job in workflow.jobs:
            for scalar in job.python_versions:
                result = interpret_python_version(scalar.raw, source=scalar.source)
                if isinstance(result, NonConcretePythonVersion):
                    notices.append(f"non-concrete: {scalar.raw} ({result.reason}; {workflow.path})")
                else:
                    matrix.setdefault(result.as_version(), "matrix versions")
    if not concrete:
        concrete = matrix
    return concrete, list(dict.fromkeys(notices))


def _lockfile_kind(candidate: dict[str, Any], path: str) -> str:
    configured = candidate.get("lockfile_kind")
    if isinstance(configured, str) and configured in SUPPORTED_KINDS:
        return configured
    if path.endswith("uv.lock"):
        return "uv.lock"
    if path.endswith("poetry.lock"):
        return "poetry.lock"
    return "requirements"


def _base_pins(
    client: GitHubClient,
    candidate: dict[str, Any],
    base_repo: str,
) -> tuple[list[tuple[str, str]], list[str]]:
    pins: list[tuple[str, str]] = []
    undetermined: list[str] = []
    paths = candidate.get("lockfile_paths")
    if not isinstance(paths, list) or not paths:
        return pins, ["pins could not be determined"]
    base_sha = str(candidate.get("base_sha") or "")
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        text = _fetch_raw(client, base_repo, path, base_sha)
        if text is None:
            undetermined.append(f"{path}: pins could not be determined")
            continue
        try:
            parsed = extract_pinned_versions(text, _lockfile_kind(candidate, path))
        except LockParseError:
            undetermined.append(f"{path}: pins could not be determined")
            continue
        if not parsed:
            undetermined.append(f"{path}: pins could not be determined")
            continue
        for name, versions in parsed.items():
            for version in versions:
                pins.append((name, version))
    pins = sorted(set(pins))
    return pins, undetermined


def _base_contamination_lines(
    client: GitHubClient,
    candidate: dict[str, Any],
    base_repo: str,
    pypi_fetch: Callable[[str], bytes] | None,
    cache: PypiCache,
    max_pypi_lookups: int,
    targets: dict[Version, str],
    check_notices: list[str],
) -> list[str]:
    if not targets:
        notices = [f"  {item}; not evaluated" for item in check_notices]
        return [_BASE_NO_FINDING, *notices]
    pins, undetermined = _base_pins(client, candidate, base_repo)
    notices: list[str] = list(undetermined)
    if max_pypi_lookups < len(pins):
        notices.insert(0, f"base check truncated: {max_pypi_lookups} of {len(pins)} pins checked")
    checked = pins[:max_pypi_lookups]
    findings: list[str] = []
    for name, version in checked:
        info = _fetch_pypi(client, name, version, pypi_fetch, cache)
        if info is None or not info.requires_python:
            continue
        for target, label in sorted(targets.items()):
            if allows_python(info.requires_python, target) is False:
                findings.append(
                    f"  {name}=={version} requires_python {info.requires_python} excludes "
                    f"Python {target} ({label})"
                )
    for item in check_notices:
        notices.append(f"  {item}; not evaluated")
    if not findings:
        return [_BASE_NO_FINDING, *notices]
    return ["base already incompatible:", *notices, *dict.fromkeys(findings)]


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _commit_time(client: GitHubClient, repo: str, sha: str) -> datetime | None:
    if not repo or not sha:
        return None
    url = f"{github.API_ROOT}/repos/{repo}/commits/{sha}"
    try:
        payload = client.get_json(url)
        date = payload["commit"]["committer"]["date"]
    except (GitHubError, OSError, ValueError, KeyError, TypeError):
        return None
    return _parse_timestamp(date)


def _pr_merge_time(client: GitHubClient, repo: str, record: dict[str, Any]) -> datetime | None:
    merged = _parse_timestamp(record.get("merged_at"))
    if merged is not None:
        return merged
    number = record.get("pr_number")
    if not repo or not isinstance(number, int) or isinstance(number, bool):
        return None
    url = f"{github.API_ROOT}/repos/{repo}/pulls/{number}"
    try:
        payload = client.get_json(url)
    except (GitHubError, OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return _parse_timestamp(payload.get("merged_at"))


def _sibling_records(directory: Path, also_dirs: list[Path] | None) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for candidate_dir in (directory, *(also_dirs or ())):
        for name in ("candidates.jsonl", "excluded.jsonl"):
            path = candidate_dir / name
            if not path.is_file():
                continue
            for case_id, record in load_candidates(path).items():
                records.setdefault(case_id, record)
    return records


def _format_siblings(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "none"
    parts = []
    for entry in sorted(entries, key=lambda item: (str(item["time"]), str(item["case_id"]))):
        merged = entry["time"].isoformat().replace("+00:00", "Z")
        title = str(entry.get("title") or "")
        suffix = f": {title}" if title else ""
        parts.append(f"{entry['case_id']} ({merged}){suffix}")
    return ", ".join(parts)


def _sibling_pr_lines(
    client: GitHubClient,
    candidate: dict[str, Any],
    *,
    directory: Path,
    also_dirs: list[Path] | None,
) -> list[str]:
    repo = str(candidate.get("repo") or "")
    current_case = str(candidate.get("case_id") or "")
    base_time = _commit_time(client, repo, str(candidate.get("base_sha") or ""))
    merge_time = _parse_timestamp(candidate.get("merged_at"))
    records = _sibling_records(directory, also_dirs)
    before_base: list[dict[str, Any]] = []
    before_merge: list[dict[str, Any]] = []
    for case_id, record in records.items():
        if case_id == current_case or str(record.get("repo") or "") != repo:
            continue
        merged = _pr_merge_time(client, repo, record)
        if merged is None:
            continue
        entry = {"case_id": case_id, "time": merged, "title": record.get("title")}
        for anchor, bucket in ((base_time, before_base), (merge_time, before_merge)):
            if anchor is None:
                continue
            if anchor - timedelta(hours=24) <= merged <= anchor:
                bucket.append(entry)
    base_text = "unavailable" if base_time is None else _format_siblings(before_base)
    merge_text = "unavailable" if merge_time is None else _format_siblings(before_merge)
    return [
        f"sibling PRs before base (hint only): {base_text}",
        f"sibling PRs before merged_at (hint only): {merge_text}",
    ]


def _render_case_report(
    candidate: dict[str, Any],
    *,
    dir: Path,
    also_dirs: list[Path] | None,
    active_client: GitHubClient,
    pypi_fetch: Callable[[str], bytes] | None,
    cache: PypiCache,
    max_pypi_lookups: int,
    include_lockfile_diff: bool = True,
    include_workflow_runs: bool = True,
) -> list[str]:
    package = str(candidate.get("package") or "")
    old_version = candidate.get("old_version")
    new_version = candidate.get("new_version")
    old_version = old_version if isinstance(old_version, str) else None
    new_version = new_version if isinstance(new_version, str) else None

    old_info = _fetch_pypi(active_client, package, old_version, pypi_fetch, cache)
    new_info = _fetch_pypi(active_client, package, new_version, pypi_fetch, cache)
    base_repo, head_repo = _resolve_repos(active_client, candidate)
    head_sha = str(candidate.get("head_sha") or "")
    workflows, failures, listing_ok = _fetch_workflows(active_client, head_repo, head_sha)
    repo_requires_python = _fetch_requires_python(active_client, head_repo, head_sha)
    targets, check_notices = _failing_targets(candidate, workflows)

    lines = _header_lines(candidate)
    lines.extend(_pypi_lines(old_info, new_info))
    lines.extend(
        _workflow_lines(workflows, failures, listing_ok, include_runs=include_workflow_runs)
    )
    lines.extend(
        _lockfile_lines(
            active_client,
            candidate,
            base_repo,
            head_repo,
            package,
            include_diff=include_lockfile_diff,
        )
    )
    if repo_requires_python:
        lines.append(f"repo requires-python: {repo_requires_python}")
    lines.extend(
        _verdict_lines(
            workflows,
            old_info,
            new_info,
            old_version is not None,
            repo_requires_python,
            targets,
        )
    )
    lines.extend(_failing_check_lines(candidate, workflows))
    lines.extend(
        _base_contamination_lines(
            active_client,
            candidate,
            base_repo,
            pypi_fetch,
            cache,
            max(0, max_pypi_lookups),
            targets,
            check_notices,
        )
    )
    lines.extend(
        _sibling_pr_lines(
            active_client,
            candidate,
            directory=dir,
            also_dirs=also_dirs,
        )
    )
    return lines


def inspect_case(
    case_id: str,
    *,
    dir: Path,
    also_dirs: list[Path] | None = None,
    offline: bool = False,
    cache_dir: Path | None = None,
    client: GitHubClient | None = None,
    pypi_fetch: Callable[[str], bytes] | None = None,
    max_rate_limit_wait_s: float = 900.0,
    max_pypi_lookups: int = MAX_PYPI_LOOKUPS,
    pypi_cache: PypiCache | None = None,
) -> str:
    """Render a read-only inspection report for one candidate."""
    candidate, _ = find_candidate(case_id, dir, also_dirs)
    active_client = client or GitHubClient(
        cache_dir=cache_dir,
        offline=offline,
        max_rate_limit_wait_s=max_rate_limit_wait_s,
    )
    lines = _render_case_report(
        candidate,
        dir=dir,
        also_dirs=also_dirs,
        active_client=active_client,
        pypi_fetch=pypi_fetch,
        cache=pypi_cache if pypi_cache is not None else {},
        max_pypi_lookups=max_pypi_lookups,
    )
    return "\n".join(redact(line) for line in lines) + "\n"


def run_inspect_batch(
    directory: Path,
    *,
    out: Path,
    only: str | None = None,
    case_ids: list[str] | None = None,
    offline: bool = False,
    cache_dir: Path | None = None,
    client: GitHubClient | None = None,
    pypi_fetch: Callable[[str], bytes] | None = None,
    max_rate_limit_wait_s: float = 900.0,
    max_pypi_lookups: int = MAX_PYPI_LOOKUPS,
) -> Path:
    """Write a read-only inspection report for selected cases."""
    directory = Path(directory)
    candidates = load_candidates(directory / "candidates.jsonl")
    if case_ids is not None:
        unknown = [case_id for case_id in case_ids if case_id not in candidates]
        if unknown:
            raise ValueError("candidate not found: " + ", ".join(unknown))
    selected = sorted(candidates)
    if case_ids is not None:
        selected = [case_id for case_id in selected if case_id in set(case_ids)]
    if only is not None:
        selected = [
            case_id for case_id in selected if candidates[case_id].get("label_suggestion") == only
        ]
    active_client = client or GitHubClient(
        cache_dir=cache_dir,
        offline=offline,
        max_rate_limit_wait_s=max_rate_limit_wait_s,
        prefer_cache=True,
        reuse_responses=True,
    )
    cache: PypiCache = {}
    output: list[str] = []
    for case_id in selected:
        output.extend([f"=== {case_id} ==="])
        try:
            lines = _render_case_report(
                candidates[case_id],
                dir=directory,
                also_dirs=None,
                active_client=active_client,
                pypi_fetch=pypi_fetch,
                cache=cache,
                max_pypi_lookups=max_pypi_lookups,
                include_lockfile_diff=False,
                include_workflow_runs=False,
            )
        except GitHubFatalError:
            raise
        except (GitHubError, OSError, ValueError, KeyError, TypeError) as exc:
            output.append(f"error: {type(exc).__name__}: {redact(str(exc))}")
        else:
            output.extend(redact(line) for line in lines)
        output.append("")
    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".tmp")
    temp.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
    temp.replace(destination)
    return destination
