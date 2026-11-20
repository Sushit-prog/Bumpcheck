from __future__ import annotations

import base64
import json
import urllib.error
from pathlib import Path
from typing import Any, Callable

import pytest
from typer.testing import CliRunner

from bumpcheck.bench.github import GitHubClient, HttpStatusError
from bumpcheck.bench.inspect_case import PYPI_JSON_URL, inspect_case
from bumpcheck.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "workflows" / "pylint.yml"
UNQUOTED_FIXTURE = Path(__file__).parent / "fixtures" / "workflows" / "unquoted_python.yml"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com/acme/widgets"
REPO = "acme/widgets"
PR_URL = f"{API}/repos/{REPO}/pulls/1"
WORKFLOWS_URL = f"{API}/repos/{REPO}/contents/.github/workflows?ref=head-sha"
WORKFLOW_URL = f"{API}/repos/{REPO}/contents/.github/workflows/pylint.yml?ref=head-sha"
PYPROJECT_URL = f"{RAW}/head-sha/pyproject.toml"
OLD_LOCK_URL = f"{RAW}/base-sha/requirements.txt"
NEW_LOCK_URL = f"{RAW}/head-sha/requirements.txt"


def _candidate(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "case_id": "acme/widgets#1",
        "repo": REPO,
        "pr_number": 1,
        "package": "requests",
        "old_version": "2.31.0",
        "new_version": "2.32.0",
        "base_sha": "base-sha",
        "head_sha": "head-sha",
        "merged_at": "2026-01-05T10:00:00Z",
        "lockfile_paths": ["requirements.txt"],
        "ci_checks": [],
    }
    record.update(overrides)
    return record


def _setup(tmp_path: Path, *candidates: dict[str, Any], name: str = "store") -> Path:
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    (directory / "candidates.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in candidates),
        encoding="utf-8",
    )
    return directory


def _pypi_url(version: str) -> str:
    return PYPI_JSON_URL.format(name="requests", version=version)


def _pypi_bytes(
    version: str,
    requires_python: str | None,
    requires_dist: list[str] | None = None,
) -> bytes:
    return json.dumps(
        {
            "info": {
                "name": "requests",
                "version": version,
                "requires_python": requires_python,
                "requires_dist": requires_dist,
            }
        }
    ).encode("utf-8")


def _pypi_fetch(responses: dict[str, bytes | BaseException]) -> Callable[[str], bytes]:
    def fetch(url: str) -> bytes:
        if url not in responses:
            raise AssertionError(f"Unexpected PyPI URL: {url}")
        value = responses[url]
        if isinstance(value, BaseException):
            raise value
        return value

    return fetch


def _workflow_json(workflow_text: str) -> dict[str, Any]:
    content = base64.b64encode(workflow_text.encode("utf-8")).decode("ascii")
    return {
        PR_URL: {
            "base": {"full_name": REPO},
            "head": {"full_name": REPO},
        },
        WORKFLOWS_URL: [{"type": "file", "name": "pylint.yml", "url": WORKFLOW_URL}],
        WORKFLOW_URL: {"content": content},
    }


def _responses(
    *,
    workflow_text: str | None = None,
    pyproject: str = '[project]\nrequires-python = ">=3.8"\n',
    old_lock: str = "requests==2.31.0\nunrelated==1.0.0\n",
    new_lock: str = "requests==2.32.0\nunrelated==2.0.0\n",
) -> tuple[dict[str, Any], dict[str, bytes]]:
    text = workflow_text if workflow_text is not None else FIXTURE.read_text(encoding="utf-8")
    json_responses = _workflow_json(text)
    bytes_responses = {
        PYPROJECT_URL: pyproject.encode("utf-8"),
        OLD_LOCK_URL: old_lock.encode("utf-8"),
        NEW_LOCK_URL: new_lock.encode("utf-8"),
    }
    return json_responses, bytes_responses


def _default_pypi() -> dict[str, bytes | BaseException]:
    return {
        _pypi_url("2.31.0"): _pypi_bytes("2.31.0", ">=3.8", ["requests>=2.31.0"]),
        _pypi_url("2.32.0"): _pypi_bytes("2.32.0", ">=3.10", ["requests>=2.32.0"]),
    }


class FakeGitHub:
    def __init__(
        self,
        json_responses: dict[str, Any],
        bytes_responses: dict[str, bytes],
    ) -> None:
        self.json_responses = json_responses
        self.bytes_responses = bytes_responses
        self.json_calls: list[str] = []
        self.bytes_calls: list[str] = []

    def get_json(self, url: str) -> Any:
        self.json_calls.append(url)
        if url not in self.json_responses:
            raise AssertionError(f"Unexpected JSON URL: {url}")
        value = self.json_responses[url]
        if isinstance(value, BaseException):
            raise value
        return value

    def get_bytes(self, url: str) -> bytes:
        self.bytes_calls.append(url)
        if url not in self.bytes_responses:
            raise AssertionError(f"Unexpected bytes URL: {url}")
        value = self.bytes_responses[url]
        if isinstance(value, BaseException):
            raise value
        return value

    def get_external_bytes(self, url: str, *, timeout: float = 10) -> bytes:
        raise AssertionError(f"Unexpected external fetch: {url}")


def _inspect(
    tmp_path: Path,
    candidate: dict[str, Any],
    *,
    json_responses: dict[str, Any] | None = None,
    bytes_responses: dict[str, bytes] | None = None,
    pypi: dict[str, bytes | BaseException] | None = None,
    also_dirs: list[Path] | None = None,
) -> str:
    directory = _setup(tmp_path, candidate)
    if json_responses is None or bytes_responses is None:
        default_json, default_bytes = _responses()
        json_responses = default_json if json_responses is None else json_responses
        bytes_responses = default_bytes if bytes_responses is None else bytes_responses
    client = FakeGitHub(json_responses, bytes_responses)
    return inspect_case(
        candidate["case_id"],
        dir=directory,
        also_dirs=also_dirs,
        client=client,  # type: ignore[arg-type]
        pypi_fetch=_pypi_fetch(pypi if pypi is not None else _default_pypi()),
    )


def test_pypi_metadata_and_dependency_diff(tmp_path: Path) -> None:
    pypi = {
        _pypi_url("2.31.0"): _pypi_bytes(
            "2.31.0",
            ">=3.8",
            ["requests>=2.31.0", "olddep<2", "changed>=1"],
        ),
        _pypi_url("2.32.0"): _pypi_bytes(
            "2.32.0",
            ">=3.10",
            ["requests>=2.32.0", "newdep>=2", "changed>=2"],
        ),
    }
    report = _inspect(tmp_path, _candidate(), pypi=pypi)
    assert "old requires_python: >=3.8" in report
    assert "new requires_python: >=3.10" in report
    assert "+ newdep>=2" in report
    assert "- olddep<2" in report
    assert "~ changed: changed>=1 -> changed>=2" in report
    assert "~ requests: requests>=2.31.0 -> requests>=2.32.0" in report


def test_workflow_matrix_and_run_lines_from_fixture(tmp_path: Path) -> None:
    report = _inspect(tmp_path, _candidate())
    assert "on: pull_request, push" in report
    assert "name: Pylint" in report
    assert "python-version matrix: 3.8, 3.9, 3.10" in report
    assert "- pip install -r requirements.txt" in report
    assert "- uv pip install pylint ruff" in report
    assert "- pylint src tests" in report
    assert "- ruff check ." in report
    assert "- pytest -q" in report


def test_python_allowed_verdict_hints(tmp_path: Path) -> None:
    pyproject = '[project]\nrequires-python = ">=3.11"\n'
    json_responses, bytes_responses = _responses(pyproject=pyproject)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "repo requires-python: >=3.11" in report
    assert "Python 3.8: new version NOT allowed (requires >=3.10); old version allowed" in report
    assert "Python 3.9: new version NOT allowed (requires >=3.10); old version allowed" in report
    assert "Python 3.10: new version allowed; old version allowed" in report
    assert "Python 3.11: new version allowed; old version allowed" in report


@pytest.mark.parametrize(
    "error",
    [
        HttpStatusError(404, _pypi_url("2.31.0"), b""),
        urllib.error.URLError("network down"),
    ],
)
def test_pypi_unavailable_on_404_or_network_error(tmp_path: Path, error: BaseException) -> None:
    pypi = {
        _pypi_url("2.31.0"): error,
        _pypi_url("2.32.0"): error,
    }
    report = _inspect(tmp_path, _candidate(), pypi=pypi)
    assert "old requires_python: unavailable" in report
    assert "new requires_python: unavailable" in report
    assert "dependency changes: unavailable" in report
    assert "Traceback" not in report


def test_offline_cache_replay_makes_no_network_calls(tmp_path: Path) -> None:
    json_responses, bytes_responses = _responses()
    pypi = _default_pypi()
    cache_dir = tmp_path / "cache"
    writer = GitHubClient(cache_dir=cache_dir)
    for url, payload in json_responses.items():
        writer._store_cache(url, None, json.dumps(payload).encode("utf-8"))
    for url, body in bytes_responses.items():
        writer._store_cache(url, None, body)
    for url, body in pypi.items():
        if isinstance(body, BaseException):
            continue
        writer._store_cache(url, None, body)

    calls: list[str] = []

    def fail_transport(method: str, url: str, headers: Any) -> tuple[int, Any, bytes]:
        calls.append(url)
        raise AssertionError(f"Unexpected transport call: {url}")

    directory = _setup(tmp_path, _candidate())
    report = inspect_case(
        "acme/widgets#1",
        dir=directory,
        client=GitHubClient(cache_dir=cache_dir, offline=True, transport=fail_transport),
    )
    assert calls == []
    assert "case: acme/widgets#1" in report
    assert "pypi:" in report
    assert "workflows:" in report
    assert "lockfile changes:" in report


def test_lockfile_diff_keeps_only_package_lines(tmp_path: Path) -> None:
    old_lock = "requests==2.31.0\nunrelated==1.0.0\n"
    new_lock = "requests==2.32.0\nunrelated==2.0.0\n"
    json_responses, bytes_responses = _responses(old_lock=old_lock, new_lock=new_lock)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "requests==2.31.0" in report
    assert "requests==2.32.0" in report
    assert "unrelated==1.0.0" not in report
    assert "unrelated==2.0.0" not in report


def test_failing_check_inference(tmp_path: Path) -> None:
    candidate = _candidate(
        ci_checks=[
            {"name": "Pylint (3.10)", "conclusion": "failure"},
            {"name": "unknown check", "conclusion": "timed_out"},
            {"name": "docs", "conclusion": "success"},
        ]
    )
    report = _inspect(tmp_path, candidate)
    assert "Pylint (3.10) (failure) -> pylint.yml job lint [3.10]" in report
    assert "unknown check (timed_out) -> unavailable" in report
    assert "docs (success)" not in report


def test_output_has_no_query_strings_or_credentials(tmp_path: Path) -> None:
    old_lock = "requests @ https://user:pass@example.com/requests?token=old\n"
    new_lock = "requests @ https://user:pass@example.com/requests?token=new\n"
    json_responses, bytes_responses = _responses(old_lock=old_lock, new_lock=new_lock)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "token=" not in report
    assert "user:pass" not in report
    assert "https://***@example.com/requests" in report


def test_also_dir_falls_back_when_primary_misses(tmp_path: Path) -> None:
    primary = _setup(tmp_path, _candidate(case_id="other/repo#9"), name="primary")
    fallback = _setup(tmp_path, _candidate(), name="fallback")
    json_responses, bytes_responses = _responses()
    client = FakeGitHub(json_responses, bytes_responses)
    report = inspect_case(
        "acme/widgets#1",
        dir=primary,
        also_dirs=[fallback],
        client=client,  # type: ignore[arg-type]
        pypi_fetch=_pypi_fetch(_default_pypi()),
    )
    assert "case: acme/widgets#1" in report


def test_cli_inspect_missing_case_exits_two(tmp_path: Path) -> None:
    directory = _setup(tmp_path, _candidate())
    result = CliRunner().invoke(
        app,
        ["bench", "inspect", "missing#1", "--dir", str(directory), "--allow-unauthenticated"],
    )
    assert result.exit_code == 2
    assert "candidate not found: missing#1" in result.output
    assert "Traceback" not in result.output


def test_cli_inspect_prints_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bumpcheck.cli as cli_module

    monkeypatch.setattr(cli_module, "run_inspect", lambda *args, **kwargs: "report\n")
    result = CliRunner().invoke(
        app,
        ["bench", "inspect", "a/b#1", "--dir", str(tmp_path), "--allow-unauthenticated"],
    )
    assert result.exit_code == 0
    assert "report" in result.output


def test_unquoted_python_matrix_regression(tmp_path: Path) -> None:
    json_responses, bytes_responses = _responses(
        workflow_text=UNQUOTED_FIXTURE.read_text(encoding="utf-8"),
        pyproject='[project]\nrequires-python = ">=3.8"\n',
    )
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "python-version matrix: 3.8, 3.9, 3.10 (unquoted: Actions reads this as 3.1)" in report
    assert "Python 3.8: new version NOT allowed (requires >=3.10); old version allowed" in report
    assert "Python 3.9: new version NOT allowed (requires >=3.10); old version allowed" in report
    assert (
        "Python 3.10 (unquoted: Actions reads this as 3.1): new version allowed; "
        "old version allowed"
    ) in report
    assert "Python 3.1:" not in report


def test_quoted_versions_and_extended_classifications(tmp_path: Path) -> None:
    workflow_text = """\
name: Versions
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version:
          - "3.10"
          - "3.1"
          - "3.x"
          - "3.13t"
          - "3.14-dev"
          - "3.12.0-rc.1"
          - "3.11.0-beta.2"
          - ">=3.9"
          - "~3.10"
          - "3.9 - 3.11"
          - "pypy-3.10"
          - "cpython-3.9"
          - "stable"
          - "${{ inputs.v }}"
    steps:
      - run: pytest -q
"""
    json_responses, bytes_responses = _responses(workflow_text=workflow_text)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "python-version matrix: 3.10, 3.1, 3.x (non-concrete), 3.13t" in report
    assert "3.14-dev, 3.12.0-rc.1, 3.11.0-beta.2" in report
    assert ">=3.9 (non-concrete), ~3.10 (non-concrete)" in report
    assert "pypy-3.10 (non-concrete), cpython-3.9 (non-concrete)" in report
    assert "stable (non-concrete), ${{ inputs.v }} (non-concrete)" in report
    assert "3.10 (unquoted" not in report
    assert "Python 3.1: new version" in report
    assert "Python 3.x: non-concrete (wildcard); requires-python not evaluated" in report
    assert "Python >=3.9: non-concrete (range); requires-python not evaluated" in report
    assert "Python 3.9 - 3.11: non-concrete (range); requires-python not evaluated" in report
    assert (
        "Python pypy-3.10: non-concrete (implementation); requires-python not evaluated"
    ) in report
    assert "Python stable: non-concrete (alias); requires-python not evaluated" in report
    assert (
        "Python ${{ inputs.v }}: non-concrete (expression); requires-python not evaluated"
    ) in report
    assert "Python 3.0:" not in report


def test_setup_python_expression_resolves_normalized_matrix_keys(tmp_path: Path) -> None:
    workflow_text = """\
name: Resolve
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        Py_Version: ["3.12"]
        include:
          - py_version: "3.13"
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.py-version }}
      - run: pytest -q
"""
    json_responses, bytes_responses = _responses(workflow_text=workflow_text)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "python-version matrix: 3.12, 3.13" in report
    assert "${{" not in report
    assert "Python 3.13: new version" in report


def test_matrix_include_and_exclude_entries_are_reported(tmp_path: Path) -> None:
    workflow_text = """\
name: Matrix
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9"]
        include:
          - python-version: "3.10"
        exclude:
          - python-version: "3.11"
    steps:
      - run: pytest -q
"""
    json_responses, bytes_responses = _responses(workflow_text=workflow_text)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "python-version matrix: 3.9, 3.10, 3.11" in report


def test_python_version_file_key_is_non_concrete(tmp_path: Path) -> None:
    workflow_text = """\
name: File
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version-file: ".python-version"
      - run: pytest -q
"""
    json_responses, bytes_responses = _responses(workflow_text=workflow_text)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "python-version matrix: .python-version (non-concrete)" in report
    assert "Python .python-version: non-concrete (file); requires-python not evaluated" in report


def test_yaml_anchors_and_merge_keys_degrade_gracefully(tmp_path: Path) -> None:
    workflow_text = """\
name: Aliases
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    defaults: &defaults
      python-version: "3.11"
    strategy:
      matrix:
        python-version: [&py "3.10"]
        include:
          - <<: *defaults
          - python-version: *py
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pytest -q
"""
    json_responses, bytes_responses = _responses(workflow_text=workflow_text)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "python-version matrix: 3.11, 3.10" in report
    assert "pylint.yml: unavailable" not in report
    assert "Python 3.11: new version" in report
    assert "Python 3.10: new version" in report


def test_malformed_yaml_degrades_gracefully(tmp_path: Path) -> None:
    workflow_text = "jobs:\n  test:\n    strategy:\n      matrix: [3.10\n"
    json_responses, bytes_responses = _responses(workflow_text=workflow_text)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "pylint.yml: unavailable" in report
    assert "Traceback" not in report


def test_workflow_without_python_matrix_reports_none(tmp_path: Path) -> None:
    workflow_text = """\
name: Docs
on: push
jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""
    json_responses, bytes_responses = _responses(workflow_text=workflow_text)
    report = _inspect(
        tmp_path,
        _candidate(),
        json_responses=json_responses,
        bytes_responses=bytes_responses,
    )
    assert "python-version matrix: none" in report
