from __future__ import annotations

from pathlib import Path

from bumpcheck.projectpython import resolve_project_python


def write_workflow(root: Path, text: str, *, name: str = "ci.yml") -> None:
    path = root / ".github" / "workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_resolve_project_python_prefers_ci_matrix(tmp_path: Path) -> None:
    write_workflow(
        tmp_path,
        """
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.8", "3.9", "3.10"]
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
""",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    result = resolve_project_python(tmp_path)
    assert result.source == "ci_matrix"
    assert result.status == "explicit"
    assert result.versions == ("3.8", "3.9", "3.10")


def test_resolve_project_python_uses_matrix_include_and_exclude(tmp_path: Path) -> None:
    write_workflow(
        tmp_path,
        """
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.8", "3.9"]
        include:
          - python-version: "3.12"
        exclude:
          - python-version: "3.8"
""",
    )
    result = resolve_project_python(tmp_path)
    assert result.versions == ("3.9", "3.12")


def test_resolve_project_python_keeps_matrix_versions_outside_universe(tmp_path: Path) -> None:
    write_workflow(
        tmp_path,
        """
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.6", "3.7", "3.10"]
""",
    )
    result = resolve_project_python(tmp_path)
    assert result.versions == ("3.6", "3.7", "3.10")


def test_resolve_project_python_warns_on_unquoted_versions(tmp_path: Path) -> None:
    write_workflow(
        tmp_path,
        """
jobs:
  test:
    strategy:
      matrix:
        python-version: [3.8, 3.10]
""",
    )
    result = resolve_project_python(tmp_path)
    assert result.versions == ("3.8",)
    assert any("unquoted" in warning and "3.10" in warning for warning in result.warnings)


def test_resolve_project_python_warns_on_unparseable_matrix_entries(tmp_path: Path) -> None:
    write_workflow(
        tmp_path,
        """
jobs:
  test:
    strategy:
      matrix:
        python-version:
          - "pypy3.10"
          - "3.x"
          - "${{ matrix.foo }}"
          - "3.14-dev"
""",
    )
    result = resolve_project_python(tmp_path)
    assert result.status == "unknown"
    assert result.versions == ()
    assert any("pypy3.10" in warning for warning in result.warnings)
    assert any("3.x" in warning for warning in result.warnings)
    assert any("matrix.foo" in warning for warning in result.warnings)
    assert any("3.14-dev" in warning for warning in result.warnings)


def test_resolve_project_python_falls_back_to_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.9,<3.12"\n', encoding="utf-8"
    )
    result = resolve_project_python(tmp_path)
    assert result.source == "pyproject"
    assert result.status == "declared"
    assert result.declared_spec == ">=3.9,<3.12"
    assert result.versions == ("3.9", "3.10", "3.11")


def test_resolve_project_python_falls_back_to_poetry_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.9"\n', encoding="utf-8"
    )
    result = resolve_project_python(tmp_path)
    assert result.source == "pyproject"
    assert result.declared_spec == ">=3.9,<4.0"
    assert result.versions == ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14")

    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "~3.9"\n', encoding="utf-8"
    )
    result = resolve_project_python(tmp_path)
    assert result.declared_spec == ">=3.9,<3.10"
    assert result.versions == ("3.9",)


def test_resolve_project_python_falls_back_to_setup_cfg(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text("[options]\npython_requires = >=3.8\n", encoding="utf-8")
    result = resolve_project_python(tmp_path)
    assert result.source == "setup"
    assert result.status == "declared"
    assert result.versions[0] == "3.8"


def test_resolve_project_python_falls_back_to_version_file(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.11.4\n", encoding="utf-8")
    result = resolve_project_python(tmp_path)
    assert result.source == "python_version_file"
    assert result.versions == ("3.11",)


def test_resolve_project_python_unknown_when_no_sources(tmp_path: Path) -> None:
    result = resolve_project_python(tmp_path)
    assert result.source == "unknown"
    assert result.status == "unknown"
    assert result.versions == ()
