"""Static AST usage tracing for a package's symbols in a repository."""

from __future__ import annotations

import ast
import io
import os
import tokenize
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field, field_validator

HitKind = Literal["call", "attribute", "import", "reference"]
Confidence = Literal["exact", "possible"]
MatchKind = Literal["exact", "descendant", "ancestor_call", "fallback"]
SkipReason = Literal["unparsable", "too_large", "symlink", "unreadable"]

_MAX_FILES = 5000
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_SNIPPET = 200
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        "site-packages",
    }
)
_UNPARSABLE_ERRORS = (SyntaxError, ValueError, UnicodeDecodeError, RecursionError)
_DYNAMIC_IMPORT_PATHS = frozenset({"importlib.import_module", "builtins.__import__"})


class SkippedFile(BaseModel):
    """A Python file that was not scanned, and why."""

    model_config = ConfigDict(frozen=True)

    file: str
    reason: SkipReason


class UsageHit(BaseModel):
    """A place where repository code touches a requested symbol."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    file: str
    line: int
    col: int
    snippet: str
    kind: HitKind
    keywords: tuple[str, ...] = ()
    confidence: Confidence
    n_positional: int = 0
    has_star_args: bool = False
    has_star_kwargs: bool = False


class UsageResult(BaseModel):
    """The outcome of tracing symbol usage through a repository."""

    model_config = ConfigDict(frozen=True)

    package: str
    symbols: list[str]
    parameters: dict[str, list[tuple[str, str]]] = Field(default_factory=dict)
    hits: list[UsageHit] = Field(default_factory=list)
    files_scanned: int
    files_skipped: list[SkippedFile] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("package")
    @classmethod
    def _canonicalize_package(cls, value: str) -> str:
        return canonicalize_name(value)

    @field_validator("symbols")
    @classmethod
    def _sort_symbols(cls, value: list[str]) -> list[str]:
        return sorted({symbol for symbol in value if symbol})

    @field_validator("parameters")
    @classmethod
    def _sort_parameters(
        cls, value: Mapping[str, Sequence[tuple[str, str]]]
    ) -> dict[str, list[tuple[str, str]]]:
        return {
            symbol: sorted({(name, kind) for name, kind in changes})
            for symbol, changes in sorted(value.items())
        }

    @field_validator("hits")
    @classmethod
    def _sort_hits(cls, value: list[UsageHit]) -> list[UsageHit]:
        return sorted(
            value,
            key=lambda hit: (hit.file, hit.line, hit.col, hit.symbol, hit.kind),
        )

    @field_validator("files_skipped")
    @classmethod
    def _sort_skipped(cls, value: list[SkippedFile]) -> list[SkippedFile]:
        return sorted(value, key=lambda item: (item.file, item.reason))

    @field_validator("risks", "warnings")
    @classmethod
    def _sort_messages(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


@dataclass(frozen=True)
class _Candidate:
    path: str
    confidence: Confidence


def _default_import_name(package: str) -> str:
    return canonicalize_name(package).replace("-", "_")


def _normalize_import_names(package: str, import_names: Sequence[str] | None) -> list[str]:
    if import_names:
        names = [name.strip() for name in import_names if name and name.strip()]
        if names:
            return sorted(set(names))
    return [_default_import_name(package)]


def _under_root(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(f"{root}.") for root in roots)


_Relation = Literal["exact", "descendant", "ancestor", "fallback"]


def _relation(path: str, target: str, roots: Sequence[str] = ()) -> _Relation | None:
    if path == target:
        return "exact"
    if path.startswith(f"{target}."):
        return "descendant"
    if target.startswith(f"{path}."):
        return "ancestor"
    if path.rsplit(".", 1)[-1] == target.rsplit(".", 1)[-1] and (
        not roots or _under_root(path, roots)
    ):
        return "fallback"
    return None


def match_hit_to_change(hit: UsageHit, change_symbol: str) -> MatchKind | None:
    """Match a traced usage hit to one changed symbol."""
    relation = _relation(hit.symbol, change_symbol)
    if relation == "exact":
        return "exact"
    if relation == "descendant":
        return "descendant"
    if relation == "ancestor":
        return "ancestor_call" if hit.kind == "call" else None
    if relation == "fallback":
        return "fallback"
    return None


def _related(path: str, target: str, roots: Sequence[str]) -> bool:
    return _relation(path, target, roots) is not None


def _has_capitalized_middle_segment(symbol: str) -> bool:
    parts = symbol.split(".")
    return any(part[:1].isupper() for part in parts[:-1])


class _TargetMatcher:
    def __init__(self, symbols: Sequence[str], roots: Sequence[str]) -> None:
        self.symbols = sorted(set(symbols))
        self.roots = tuple(roots)

    def match(self, path: str, *, possible: bool = False) -> tuple[str, Confidence] | None:
        if path in self.symbols:
            return path, "possible" if possible else "exact"
        related: list[str] = []
        fallback: list[str] = []
        for symbol in self.symbols:
            relation = _relation(path, symbol, self.roots)
            if relation in ("descendant", "ancestor"):
                related.append(symbol)
            elif relation == "fallback":
                fallback.append(symbol)
        if related:
            target = sorted(related, key=lambda symbol: (abs(len(symbol) - len(path)), symbol))[0]
            return target, "possible"
        if fallback:
            return sorted(fallback)[0], "possible"
        return None

    def related(self, path: str) -> bool:
        return self.match(path) is not None


class _ImportCollector(ast.NodeVisitor):
    def __init__(self, owner: _FileVisitor) -> None:
        self.owner = owner

    def _mark_type_checking(self) -> None:
        if self.owner.type_checking_depth:
            self.owner.mark_type_checking()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.owner.import_table[alias.asname] = alias.name
            else:
                root = alias.name.split(".", 1)[0]
                self.owner.import_table[root] = root
            self.owner.record(
                alias.name,
                alias.lineno,
                alias.col_offset,
                "import",
            )
            self._mark_type_checking()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level and node.level > 0:
            return
        module = node.module
        if not module:
            return
        if any(alias.name == "*" for alias in node.names):
            self.owner.star_modules.append(module)
            self.owner.risks.add("star_import")
            self.owner.record(
                module,
                node.lineno,
                node.col_offset,
                "import",
                possible=True,
            )
            self._mark_type_checking()
            return
        for alias in node.names:
            path = f"{module}.{alias.name}"
            self.owner.import_table[alias.asname or alias.name] = path
            self.owner.record(path, alias.lineno, alias.col_offset, "import")
            self._mark_type_checking()

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_test(node.test):
            self.owner.type_checking_depth += 1
            for child in node.body:
                self.visit(child)
            self.owner.type_checking_depth -= 1
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)


def _is_type_checking_test(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    return isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"


class _FileVisitor(ast.NodeVisitor):
    def __init__(
        self,
        file: str,
        source_lines: list[str],
        targets: _TargetMatcher,
        roots: Sequence[str],
    ) -> None:
        self.file = file
        self.source_lines = source_lines
        self.targets = targets
        self.roots = tuple(roots)
        self.import_table: dict[str, str] = {}
        self.star_modules: list[str] = []
        self.scopes: list[dict[str, str]] = [{}]
        self.hits: list[UsageHit] = []
        self.risks: set[str] = set()
        self.warnings: list[str] = []
        self.type_checking_depth = 0
        self._type_checking_warned = False
        self._seen: set[tuple[str, str, int, int, str]] = set()

    def run(self, tree: ast.AST) -> None:
        _ImportCollector(self).visit(tree)
        self.visit(tree)

    def mark_type_checking(self) -> None:
        if not self._type_checking_warned:
            self.warnings.append(f"{self.file}: imports under TYPE_CHECKING are still recorded")
            self._type_checking_warned = True

    def record(
        self,
        symbol: str,
        line: int,
        col: int,
        kind: HitKind,
        *,
        keywords: tuple[str, ...] = (),
        possible: bool = False,
        n_positional: int = 0,
        has_star_args: bool = False,
        has_star_kwargs: bool = False,
    ) -> None:
        match = self.targets.match(symbol, possible=possible)
        if match is None:
            return
        key = (symbol, self.file, line, col, kind)
        if key in self._seen:
            return
        self._seen.add(key)
        snippet = ""
        if 1 <= line <= len(self.source_lines):
            snippet = self.source_lines[line - 1].strip()[:_MAX_SNIPPET]
        self.hits.append(
            UsageHit(
                symbol=symbol,
                file=self.file,
                line=line,
                col=col,
                snippet=snippet,
                kind=kind,
                keywords=keywords,
                confidence=match[1],
                n_positional=n_positional,
                has_star_args=has_star_args,
                has_star_kwargs=has_star_kwargs,
            )
        )

    def _resolve(self, node: ast.expr) -> list[_Candidate]:
        if isinstance(node, ast.Name):
            if node.id in self.import_table:
                return [_Candidate(self.import_table[node.id], "exact")]
            candidates = []
            for module in self.star_modules:
                candidate = f"{module}.{node.id}"
                if self.targets.related(candidate):
                    candidates.append(_Candidate(candidate, "possible"))
            return candidates
        if isinstance(node, ast.Attribute):
            bases = self._resolve(node.value)
            if isinstance(node.value, ast.Name):
                instance = self.scopes[-1].get(node.value.id)
                if instance is not None:
                    bases.append(_Candidate(instance, "possible"))
            return [
                _Candidate(
                    f"{base.path}.{node.attr}",
                    "possible" if base.confidence == "possible" else "exact",
                )
                for base in bases
            ]
        return []

    def _instance_target(self, node: ast.expr) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        for candidate in self._resolve(node.func):
            match = self.targets.match(candidate.path, possible=candidate.confidence == "possible")
            if match is not None:
                return candidate.path
        return None

    def _bind(self, target: ast.expr, symbol: str | None) -> None:
        if isinstance(target, ast.Name):
            if symbol is None:
                self.scopes[-1].pop(target.id, None)
            else:
                self.scopes[-1][target.id] = symbol
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind(element, None)

    def _is_package_path(self, path: str) -> bool:
        return _under_root(path, self.roots)

    def _is_builtin_call(self, node: ast.expr, name: str) -> bool:
        return isinstance(node, ast.Name) and node.id == name and name not in self.import_table

    def _is_dynamic_import(self, node: ast.expr) -> bool:
        if self._is_builtin_call(node, "__import__"):
            return True
        return any(candidate.path in _DYNAMIC_IMPORT_PATHS for candidate in self._resolve(node))

    def _package_literal(self, value: str) -> bool:
        return _under_root(value, self.roots)

    def _handle_getattr(self, node: ast.Call) -> bool:
        if len(node.args) < 2:
            return False
        bases = self._resolve(node.args[0])
        if isinstance(node.args[0], ast.Name):
            instance = self.scopes[-1].get(node.args[0].id)
            if instance is not None:
                bases.append(_Candidate(instance, "possible"))
        name_node = node.args[1]
        if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
            matched = False
            for base in bases:
                symbol = f"{base.path}.{name_node.value}"
                if self.targets.match(symbol):
                    self.record(
                        symbol,
                        node.lineno,
                        node.col_offset,
                        "attribute",
                        possible=base.confidence == "possible",
                    )
                    matched = True
            return matched
        if any(self._is_package_path(base.path) for base in bases):
            self.risks.add("dynamic_getattr")
        return False

    def _handle_dynamic_import(self, node: ast.Call) -> None:
        name_node = node.args[0] if node.args else None
        if name_node is None:
            for keyword in node.keywords:
                if keyword.arg == "name":
                    name_node = keyword.value
                    break
        if name_node is None:
            return
        if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
            value = name_node.value
            if self._package_literal(value):
                self.risks.add("dynamic_import")
                return
            package_node = node.args[1] if len(node.args) > 1 else None
            if package_node is None:
                for keyword in node.keywords:
                    if keyword.arg == "package":
                        package_node = keyword.value
                        break
            if (
                value.startswith(".")
                and isinstance(package_node, ast.Constant)
                and isinstance(package_node.value, str)
                and self._package_literal(package_node.value)
            ):
                self.risks.add("dynamic_import")
            return
        self.risks.add("dynamic_import_unknown_target")

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        symbol = self._instance_target(node.value)
        for target in node.targets:
            self._bind(target, symbol)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        symbol = self._instance_target(node.value) if node.value is not None else None
        self._bind(node.target, symbol)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._bind(node.target, None)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind(node.target, self._instance_target(node.value))

    def _visit_function_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        for argument in [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]:
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.args.vararg is not None and node.args.vararg.annotation is not None:
            self.visit(node.args.vararg.annotation)
        if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
            self.visit(node.args.kwarg.annotation)
        if node.returns is not None:
            self.visit(node.returns)

    def _visit_function_body(self, body: list[ast.stmt]) -> None:
        self.scopes.append({})
        for statement in body:
            self.visit(statement)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_header(node)
        self._visit_function_body(node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_header(node)
        self._visit_function_body(node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        self.scopes.append({})
        self.visit(node.body)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._visit_function_body(node.body)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        for candidate in self._resolve(node):
            self.record(
                candidate.path,
                node.lineno,
                node.col_offset,
                "reference",
                possible=candidate.confidence == "possible",
            )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        candidates = self._resolve(node)
        matched = False
        for candidate in candidates:
            if self.targets.match(candidate.path, possible=candidate.confidence == "possible"):
                self.record(
                    candidate.path,
                    node.lineno,
                    node.col_offset,
                    "attribute",
                    possible=candidate.confidence == "possible",
                )
                matched = True
        if not matched:
            self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_builtin_call(node.func, "getattr"):
            recorded = self._handle_getattr(node)
            for argument in node.args[2:] if recorded else node.args:
                self.visit(argument)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        if self._is_dynamic_import(node.func):
            self._handle_dynamic_import(node)
        else:
            candidates = self._resolve(node.func)
            keywords = tuple(
                sorted({keyword.arg for keyword in node.keywords if keyword.arg is not None})
            )
            n_positional = sum(1 for argument in node.args if not isinstance(argument, ast.Starred))
            has_star_args = any(isinstance(argument, ast.Starred) for argument in node.args)
            has_star_kwargs = any(keyword.arg is None for keyword in node.keywords)
            matched = False
            for candidate in candidates:
                if self.targets.match(candidate.path, possible=candidate.confidence == "possible"):
                    self.record(
                        candidate.path,
                        node.lineno,
                        node.col_offset,
                        "call",
                        keywords=keywords,
                        possible=candidate.confidence == "possible",
                        n_positional=n_positional,
                        has_star_args=has_star_args,
                        has_star_kwargs=has_star_kwargs,
                    )
                    matched = True
            if not matched:
                self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Import(self, node: ast.Import) -> None:
        return

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        return


def _load_project_name(repo: Path) -> str | None:
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    name = project.get("name")
    return name if isinstance(name, str) else None


def _package_source_dirs(repo: Path, package: str, roots: Sequence[str]) -> set[Path]:
    project_name = _load_project_name(repo)
    if project_name is None or canonicalize_name(project_name) != package:
        return set()
    directories: set[Path] = set()
    for base in (repo / "src", repo):
        if not base.is_dir():
            continue
        for root in roots:
            candidate = base / root
            if candidate.is_dir():
                directories.add(candidate.resolve())
    return directories


def _iter_python_files(repo: Path, package_dirs: set[Path]) -> list[Path]:
    paths: list[Path] = []
    for current, dirnames, filenames in os.walk(repo, topdown=True, followlinks=False):
        current_path = Path(current)
        kept = []
        for dirname in sorted(dirnames):
            directory = current_path / dirname
            if dirname in _SKIP_DIRS:
                continue
            if directory.is_symlink():
                continue
            if directory.resolve() in package_dirs:
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                paths.append(current_path / filename)
    return paths


def _decode_source(data: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding)


def _parse_error_message(exc: Exception) -> str:
    if isinstance(exc, SyntaxError):
        return f"{exc.msg} (line {exc.lineno})"
    return f"{type(exc).__name__}: {exc}"


def trace_usage(
    repo_path: Path,
    package: str,
    symbols: Sequence[str],
    *,
    import_names: Sequence[str] | None = None,
    parameters: Mapping[str, Sequence[tuple[str, str]]] | None = None,
) -> UsageResult:
    """Trace static usages of ``symbols`` in the Python files under ``repo_path``."""
    repo = Path(repo_path).resolve()
    package_name = canonicalize_name(package)
    roots = _normalize_import_names(package_name, import_names)
    normalized_symbols = sorted({symbol.strip() for symbol in symbols if symbol.strip()})
    targets = _TargetMatcher(normalized_symbols, roots)
    package_dirs = _package_source_dirs(repo, package_name, roots)

    warnings: list[str] = []
    risks: set[str] = set()
    skipped: list[SkippedFile] = []
    hits: list[UsageHit] = []
    files_scanned = 0

    paths = _iter_python_files(repo, package_dirs)
    if len(paths) > _MAX_FILES:
        warnings.append(
            f"File cap reached: scanned first {_MAX_FILES} of {len(paths)} Python files"
        )
        risks.add("scan_truncated")
        paths = paths[:_MAX_FILES]

    for path in paths:
        try:
            relative = path.relative_to(repo).as_posix()
        except ValueError:
            relative = path.name
        if path.is_symlink():
            skipped.append(SkippedFile(file=relative, reason="symlink"))
            warnings.append(f"{relative}: symlink skipped")
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                skipped.append(SkippedFile(file=relative, reason="too_large"))
                warnings.append(f"{relative}: file exceeds {_MAX_FILE_BYTES} bytes")
                continue
            data = path.read_bytes()
        except OSError as exc:
            skipped.append(SkippedFile(file=relative, reason="unreadable"))
            warnings.append(f"{relative}: {type(exc).__name__}: {exc}")
            continue
        if len(data) > _MAX_FILE_BYTES:
            skipped.append(SkippedFile(file=relative, reason="too_large"))
            warnings.append(f"{relative}: file exceeds {_MAX_FILE_BYTES} bytes")
            continue
        try:
            source = _decode_source(data)
            tree = ast.parse(data, filename=relative)
        except _UNPARSABLE_ERRORS as exc:
            skipped.append(SkippedFile(file=relative, reason="unparsable"))
            warnings.append(f"{relative}: {_parse_error_message(exc)}")
            risks.add("unparsable_file")
            continue

        files_scanned += 1
        visitor = _FileVisitor(relative, source.splitlines(), targets, roots)
        visitor.run(tree)
        hits.extend(visitor.hits)
        risks.update(visitor.risks)
        warnings.extend(visitor.warnings)

    for symbol in targets.symbols:
        if _has_capitalized_middle_segment(symbol) and not any(
            _related(hit.symbol, symbol, roots) for hit in hits
        ):
            risks.add("method_target_untraced")

    return UsageResult(
        package=package_name,
        symbols=normalized_symbols,
        parameters=parameters or {},
        hits=hits,
        files_scanned=files_scanned,
        files_skipped=skipped,
        risks=risks,
        warnings=warnings,
    )
