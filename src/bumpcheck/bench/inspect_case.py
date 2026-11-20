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
from pathlib import Path
from typing import Any, Literal

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
_MATRIX_EXPRESSION_RE = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_PLAIN_FLOAT_RE = re.compile(r"(\d+)\.(\d+)")
_VERSION_KEYS = frozenset({"python-version", "python-version-file"})


@dataclass(frozen=True)
class VersionScalar:
    raw: str
    style: str | None
    source: Literal["version", "file"] = "version"


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
) -> PypiInfo | None:
    if not name or not version:
        return None
    url = _pypi_url(name, version)
    fetch = pypi_fetch if pypi_fetch is not None else client.get_external_bytes
    try:
        body = fetch(url)
        payload = json.loads(body.decode("utf-8"))
    except GitHubFatalError:
        raise
    except (GitHubError, OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
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
    return PypiInfo(
        version=fetched_version if isinstance(fetched_version, str) else version,
        requires_python=requires_python,
        requires_dist=requires_dist,
    )


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


def _normalize_key(value: object) -> str:
    return str(value).strip().lower().replace("_", "-")


def _raw_scalars(
    node: yaml.Node,
    source: Literal["version", "file"] = "version",
) -> tuple[VersionScalar, ...]:
    scalars: list[VersionScalar] = []

    def collect(current: yaml.Node) -> None:
        if isinstance(current, yaml.ScalarNode):
            scalars.append(VersionScalar(raw=current.value, style=current.style, source=source))
        elif isinstance(current, yaml.SequenceNode):
            for item in current.value:
                collect(item)
        elif isinstance(current, yaml.MappingNode):
            for _, item in current.value:
                collect(item)

    collect(node)
    return tuple(dict.fromkeys(scalars))


def _matrix_values(matrix: yaml.Node) -> dict[str, tuple[VersionScalar, ...]]:
    values: dict[str, list[VersionScalar]] = {}

    def add(key_node: yaml.Node, value_node: yaml.Node) -> None:
        if not isinstance(key_node, yaml.ScalarNode):
            return
        key = _normalize_key(key_node.value)
        values.setdefault(key, []).extend(_raw_scalars(value_node))

    def add_mapping(mapping: yaml.MappingNode) -> None:
        for key, value in mapping.value:
            if isinstance(key, yaml.ScalarNode) and key.value == "<<":
                if isinstance(value, yaml.MappingNode):
                    add_mapping(value)
                elif isinstance(value, yaml.SequenceNode):
                    for item in value.value:
                        if isinstance(item, yaml.MappingNode):
                            add_mapping(item)
                continue
            add(key, value)

    if isinstance(matrix, yaml.MappingNode):
        for key, value in matrix.value:
            normalized = _normalize_key(key.value) if isinstance(key, yaml.ScalarNode) else ""
            if normalized == "include" and isinstance(value, yaml.SequenceNode):
                for item in value.value:
                    if isinstance(item, yaml.MappingNode):
                        add_mapping(item)
            else:
                add(key, value)
    return {key: tuple(dict.fromkeys(items)) for key, items in values.items()}


def _resolve_matrix_expression(
    node: yaml.Node,
    matrix_values: dict[str, tuple[VersionScalar, ...]],
) -> tuple[VersionScalar, ...] | None:
    scalars = _raw_scalars(node)
    if len(scalars) != 1:
        return None
    match = _MATRIX_EXPRESSION_RE.fullmatch(scalars[0].raw)
    if match is None:
        return None
    return matrix_values.get(_normalize_key(match.group(1))) or None


def _python_version_values(
    job_node: yaml.Node,
    matrix_values: dict[str, tuple[VersionScalar, ...]],
) -> tuple[VersionScalar, ...]:
    scalars: list[VersionScalar] = []

    def visit(current: yaml.Node, in_matrix: bool) -> None:
        if isinstance(current, yaml.SequenceNode):
            for item in current.value:
                visit(item, in_matrix)
            return
        if not isinstance(current, yaml.MappingNode):
            return
        for key, value in current.value:
            if not isinstance(key, yaml.ScalarNode):
                visit(value, in_matrix)
                continue
            normalized = _normalize_key(key.value)
            if normalized in _VERSION_KEYS:
                source = "file" if normalized == "python-version-file" else "version"
                resolved = (
                    _resolve_matrix_expression(value, matrix_values)
                    if source == "version" and not in_matrix
                    else None
                )
                scalars.extend(resolved if resolved is not None else _raw_scalars(value, source))
                continue
            visit(value, in_matrix or normalized == "matrix")

    visit(job_node, False)
    return tuple(dict.fromkeys(scalars))


def _job_matrix_node(job_node: yaml.Node) -> yaml.Node | None:
    if not isinstance(job_node, yaml.MappingNode):
        return None
    for key, value in job_node.value:
        if not isinstance(key, yaml.ScalarNode) or _normalize_key(key.value) != "strategy":
            continue
        if not isinstance(value, yaml.MappingNode):
            continue
        for strategy_key, strategy_value in value.value:
            if (
                isinstance(strategy_key, yaml.ScalarNode)
                and _normalize_key(strategy_key.value) == "matrix"
            ):
                return strategy_value
    return None


def _composed_jobs(root: yaml.Node | None) -> dict[str, yaml.Node]:
    if not isinstance(root, yaml.MappingNode):
        return {}
    for key, value in root.value:
        if not isinstance(key, yaml.ScalarNode) or _normalize_key(key.value) != "jobs":
            continue
        if isinstance(value, yaml.MappingNode):
            return {
                job_key.value: job_value
                for job_key, job_value in value.value
                if isinstance(job_key, yaml.ScalarNode)
            }
    return {}


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


def _version_in_name(version: str, check_name: str) -> bool:
    if not version:
        return False
    return bool(re.search(rf"(?<![\d.]){re.escape(version)}(?![\d.])", check_name))


def infer_check_location(
    workflows: tuple[WorkflowFile, ...] | list[WorkflowFile], check_name: str
) -> tuple[str, str, str | None] | None:
    lowered = check_name.lower()
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
            matched_version = None
            for scalar in job.python_versions:
                result = interpret_python_version(scalar.raw, source=scalar.source)
                if not isinstance(result, ConcretePythonVersion):
                    continue
                if _version_in_name(scalar.raw, check_name):
                    score += 1
                    matched_version = scalar.raw
                    break
            if score <= 0:
                continue
            key = (-score, workflow.path, job.job_id)
            if best is None or key < (-best[0], best[1], best[2]):
                best = (score, workflow.path, job.job_id, matched_version)
    if best is None:
        return None
    return best[1], best[2], best[3]


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
    merged_at = candidate.get("merged_at") or "unavailable"
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
    lines.append("  dependency changes:")
    for line in changes.added:
        lines.append(f"    + {line}")
    for line in changes.removed:
        lines.append(f"    - {line}")
    for name, old_lines, new_lines in changes.changed:
        old_text = ", ".join(old_lines) or "none"
        new_text = ", ".join(new_lines) or "none"
        lines.append(f"    ~ {name}: {old_text} -> {new_text}")
    return lines


def _unquoted_float_warning(scalar: VersionScalar) -> str | None:
    if scalar.style is not None:
        return None
    match = _PLAIN_FLOAT_RE.fullmatch(scalar.raw)
    if match is None:
        return None
    major, minor = match.groups()
    if not minor.endswith("0"):
        return None
    as_float_minor = minor.rstrip("0") or "0"
    return f"(unquoted: Actions reads this as {major}.{as_float_minor})"


def _format_version_scalar(scalar: VersionScalar) -> str:
    result = interpret_python_version(scalar.raw, source=scalar.source)
    if isinstance(result, NonConcretePythonVersion):
        return f"{scalar.raw} (non-concrete)"
    warning = _unquoted_float_warning(scalar)
    if warning is not None:
        return f"{scalar.raw} {warning}"
    return scalar.raw


def _workflow_lines(files: list[WorkflowFile], failures: list[str], listing_ok: bool) -> list[str]:
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
        else:
            for line in diff:
                lines.append(f"    {line}")
    return lines


def _verdict_lines(
    workflows: list[WorkflowFile],
    old_info: PypiInfo | None,
    new_info: PypiInfo | None,
    old_version_available: bool,
    repo_requires_python: str | None,
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
        if location is None:
            lines.append(f"  {name} ({conclusion}) -> unavailable")
            continue
        path, job_id, version = location
        suffix = f" [{version}]" if version else ""
        lines.append(f"  {name} ({conclusion}) -> {path} job {job_id}{suffix}")
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
) -> str:
    """Render a read-only inspection report for one candidate."""
    candidate, _ = find_candidate(case_id, dir, also_dirs)
    active_client = client or GitHubClient(
        cache_dir=cache_dir,
        offline=offline,
        max_rate_limit_wait_s=max_rate_limit_wait_s,
    )
    package = str(candidate.get("package") or "")
    old_version = candidate.get("old_version")
    new_version = candidate.get("new_version")
    old_version = old_version if isinstance(old_version, str) else None
    new_version = new_version if isinstance(new_version, str) else None

    old_info = _fetch_pypi(active_client, package, old_version, pypi_fetch)
    new_info = _fetch_pypi(active_client, package, new_version, pypi_fetch)
    base_repo, head_repo = _resolve_repos(active_client, candidate)
    head_sha = str(candidate.get("head_sha") or "")
    workflows, failures, listing_ok = _fetch_workflows(active_client, head_repo, head_sha)
    repo_requires_python = _fetch_requires_python(active_client, head_repo, head_sha)

    lines = _header_lines(candidate)
    lines.extend(_pypi_lines(old_info, new_info))
    lines.extend(_workflow_lines(workflows, failures, listing_ok))
    lines.extend(_lockfile_lines(active_client, candidate, base_repo, head_repo, package))
    if repo_requires_python:
        lines.append(f"repo requires-python: {repo_requires_python}")
    lines.extend(
        _verdict_lines(
            workflows,
            old_info,
            new_info,
            old_version is not None,
            repo_requires_python,
        )
    )
    lines.extend(_failing_check_lines(candidate, workflows))
    return "\n".join(redact(line) for line in lines) + "\n"
