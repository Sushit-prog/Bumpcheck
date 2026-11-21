"""Resolve the Python versions supported by a project repository."""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from bumpcheck.pycompat import (
    CPYTHON_MINORS,
    NonConcretePythonVersion,
    interpret_python_version,
    python_floor,
)

_MATRIX_EXPRESSION_RE = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_PLAIN_FLOAT_RE = re.compile(r"(\d+)\.(\d+)")
_VERSION_KEYS = frozenset({"python-version", "python-version-file"})

ProjectPythonSource = Literal[
    "ci_matrix",
    "pyproject",
    "setup",
    "python_version_file",
    "unknown",
]
ProjectPythonStatus = Literal["explicit", "declared", "unknown"]


@dataclass(frozen=True)
class VersionScalar:
    raw: str
    style: str | None
    source: Literal["version", "file"] = "version"


@dataclass(frozen=True)
class ProjectPythonSet:
    source: ProjectPythonSource
    status: ProjectPythonStatus
    versions: tuple[str, ...] = ()
    declared_spec: str | None = None
    warnings: tuple[str, ...] = ()


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
            if normalized in {"include", "exclude"} and isinstance(value, yaml.SequenceNode):
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
                resolved = None
                if source == "version":
                    resolved = (
                        matrix_values.get(normalized)
                        if in_matrix
                        else _resolve_matrix_expression(value, matrix_values)
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


def _scalar_text(scalar: VersionScalar) -> str:
    if scalar.style == '"':
        return f'"{scalar.raw}"'
    if scalar.style == "'":
        return f"'{scalar.raw}'"
    return scalar.raw


def _format_version_scalar(scalar: VersionScalar) -> str:
    result = interpret_python_version(scalar.raw, source=scalar.source)
    if isinstance(result, NonConcretePythonVersion):
        return f"{scalar.raw} (non-concrete)"
    warning = _unquoted_float_warning(scalar)
    if warning is not None:
        return f"{scalar.raw} {warning}"
    return scalar.raw


def _matrix_exclusions(matrix: yaml.Node) -> frozenset[str]:
    if not isinstance(matrix, yaml.MappingNode):
        return frozenset()
    excluded: set[str] = set()
    for key, value in matrix.value:
        if not isinstance(key, yaml.ScalarNode) or _normalize_key(key.value) != "exclude":
            continue
        if not isinstance(value, yaml.SequenceNode):
            continue
        for item in value.value:
            if not isinstance(item, yaml.MappingNode):
                continue
            for entry_key, entry_value in item.value:
                if (
                    isinstance(entry_key, yaml.ScalarNode)
                    and _normalize_key(entry_key.value) in _VERSION_KEYS
                ):
                    excluded.update(scalar.raw for scalar in _raw_scalars(entry_value))
    return frozenset(excluded)


def _workflow_scalars(root: yaml.Node | None) -> tuple[VersionScalar, ...]:
    scalars: list[VersionScalar] = []
    for job_node in _composed_jobs(root).values():
        matrix_node = _job_matrix_node(job_node)
        matrix_values = _matrix_values(matrix_node) if matrix_node is not None else {}
        values = _python_version_values(job_node, matrix_values)
        excluded = _matrix_exclusions(matrix_node) if matrix_node is not None else frozenset()
        if excluded:
            values = tuple(scalar for scalar in values if scalar.raw not in excluded)
        scalars.extend(values)
    return tuple(dict.fromkeys(scalars))


def _workflow_scalars_from_text(
    path: Path,
    text: str,
) -> tuple[tuple[VersionScalar, ...], tuple[str, ...]]:
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        return (), (f"{path.name}: YAML parse error: {exc}",)
    return _workflow_scalars(root), ()


def _concrete_versions(
    values: tuple[VersionScalar, ...],
    warnings: list[str],
) -> tuple[str, ...]:
    versions: list[Version] = []
    for scalar in values:
        warning = _unquoted_float_warning(scalar)
        if warning is not None:
            warnings.append(f"{scalar.raw}: {warning}")
            continue
        if "dev" in scalar.raw.lower():
            warnings.append(f"{scalar.raw}: non-concrete (development version)")
            continue
        result = interpret_python_version(scalar.raw, source=scalar.source)
        if isinstance(result, NonConcretePythonVersion):
            warnings.append(f"{scalar.raw}: non-concrete ({result.reason})")
            continue
        version = result.as_version()
        if version not in versions:
            versions.append(version)
    return tuple(
        str(version) for version in sorted(versions, key=lambda item: (item.major, item.minor))
    )


def _poetry_specifier(text: str) -> str:
    value = text.strip()
    if value.startswith("^"):
        rest = value[1:].strip()
        parts = rest.split(".")
        if parts and parts[0].isdigit():
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            return f">={major}.{minor},<{major + 1}.0"
    if value.startswith("~") and not value.startswith("~="):
        rest = value[1:].strip()
        parts = rest.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            major, minor = int(parts[0]), int(parts[1])
            return f">={major}.{minor},<{major}.{minor + 1}"
    return value


def _versions_from_spec(spec: str) -> tuple[str, ...]:
    try:
        spec_set = SpecifierSet(spec)
    except InvalidSpecifier:
        return ()
    versions = [version for version in CPYTHON_MINORS if version in spec_set]
    floor = python_floor(spec)
    if floor is not None and floor not in versions:
        versions.append(floor)
    return tuple(
        str(version) for version in sorted(versions, key=lambda item: (item.major, item.minor))
    )


def _declared_project_python(
    spec: str,
    source: Literal["pyproject", "setup"],
    warnings: list[str],
) -> ProjectPythonSet | None:
    versions = _versions_from_spec(spec)
    if not versions:
        warnings.append(f"{source}: could not resolve versions from {spec!r}")
        return None
    return ProjectPythonSet(
        source=source,
        status="declared",
        versions=versions,
        declared_spec=spec,
        warnings=tuple(warnings),
    )


def _pyproject_spec(path: Path, warnings: list[str]) -> str | None:
    if not path.is_file():
        return None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        warnings.append(f"pyproject.toml: could not parse ({exc})")
        return None
    project = document.get("project")
    if isinstance(project, dict):
        value = project.get("requires-python")
        if isinstance(value, str) and value.strip():
            return value.strip()
    poetry = document.get("tool")
    if isinstance(poetry, dict):
        poetry_tool = poetry.get("poetry")
        if isinstance(poetry_tool, dict):
            dependencies = poetry_tool.get("dependencies")
            if isinstance(dependencies, dict):
                value = dependencies.get("python")
                if isinstance(value, str) and value.strip():
                    return _poetry_specifier(value)
    return None


def _setup_cfg_spec(path: Path, warnings: list[str]) -> str | None:
    if not path.is_file():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error) as exc:
        warnings.append(f"setup.cfg: could not parse ({exc})")
        return None
    if parser.has_option("options", "python_requires"):
        value = parser.get("options", "python_requires").strip()
        if value:
            return value
    return None


def _setup_py_spec(path: Path, warnings: list[str]) -> str | None:
    if not path.is_file():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        warnings.append(f"setup.py: could not parse ({exc})")
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "setup":
            continue
        for keyword in node.keywords:
            if keyword.arg != "python_requires":
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value.strip()
    return None


def resolve_project_python(repo_path: Path) -> ProjectPythonSet:
    """Resolve a project's Python set using CI, declared config, then .python-version."""
    repo = Path(repo_path)
    warnings: list[str] = []

    workflow_dir = repo / ".github" / "workflows"
    scalars: tuple[VersionScalar, ...] = ()
    if workflow_dir.is_dir():
        paths = sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")])
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                warnings.append(f"{path.name}: could not read ({exc})")
                continue
            parsed, parse_warnings = _workflow_scalars_from_text(path, text)
            warnings.extend(parse_warnings)
            scalars += parsed
    matrix_versions = _concrete_versions(scalars, warnings)
    if matrix_versions:
        return ProjectPythonSet(
            source="ci_matrix",
            status="explicit",
            versions=matrix_versions,
            warnings=tuple(warnings),
        )

    spec = _pyproject_spec(repo / "pyproject.toml", warnings)
    if spec is not None:
        resolved = _declared_project_python(spec, "pyproject", warnings)
        if resolved is not None:
            return resolved

    spec = _setup_cfg_spec(repo / "setup.cfg", warnings)
    if spec is None:
        spec = _setup_py_spec(repo / "setup.py", warnings)
    if spec is not None:
        resolved = _declared_project_python(spec, "setup", warnings)
        if resolved is not None:
            return resolved

    version_file = repo / ".python-version"
    if version_file.is_file():
        try:
            lines = version_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            warnings.append(f".python-version: could not read ({exc})")
        else:
            values = tuple(
                VersionScalar(raw=line.strip(), style=None, source="version")
                for line in lines
                if line.strip() and not line.strip().startswith("#")
            )
            file_versions = _concrete_versions(values, warnings)
            if file_versions:
                return ProjectPythonSet(
                    source="python_version_file",
                    status="explicit",
                    versions=file_versions,
                    warnings=tuple(warnings),
                )

    warnings.append("project Python set could not be resolved")
    return ProjectPythonSet(
        source="unknown",
        status="unknown",
        warnings=tuple(warnings),
    )


__all__ = [
    "CPYTHON_MINORS",
    "ProjectPythonSet",
    "VersionScalar",
    "_composed_jobs",
    "_format_version_scalar",
    "_job_matrix_node",
    "_matrix_values",
    "_normalize_key",
    "_python_version_values",
    "_raw_scalars",
    "_resolve_matrix_expression",
    "_scalar_text",
    "_unquoted_float_warning",
    "resolve_project_python",
]
