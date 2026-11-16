from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bumpcheck import usage as usage_module
from bumpcheck.apidiff import ApiChange, ApiDiffResult
from bumpcheck.cli import app
from bumpcheck.usage import SkippedFile, trace_usage

FIXTURE = Path(__file__).parent / "fixtures" / "usage_repo"


def trace(symbols: list[str], **kwargs: object):
    return trace_usage(FIXTURE, "requests", symbols, **kwargs)


def write_python(repo: Path, name: str, source: str) -> Path:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_alias_import_and_from_import() -> None:
    result = trace(["requests.sessions.Session"])

    calls = [hit for hit in result.hits if hit.file == "app/aliases.py" and hit.kind == "call"]
    assert {(hit.symbol, hit.line, hit.confidence) for hit in calls} == {
        ("requests.sessions.Session", 4, "exact"),
        ("requests.sessions.Session", 5, "exact"),
    }
    imports = [hit for hit in result.hits if hit.file == "app/aliases.py" and hit.kind == "import"]
    assert any(hit.line == 1 and hit.confidence == "possible" for hit in imports)
    assert any(hit.line == 2 and hit.confidence == "exact" for hit in imports)


def test_attribute_chains_and_re_export_fallback() -> None:
    chain_result = trace(["requests.sessions.Session.request"])

    attributes = [
        hit for hit in chain_result.hits if hit.file == "app/chains.py" and hit.kind == "attribute"
    ]
    assert any(
        hit.symbol == "requests.sessions.Session.request"
        and hit.line == 3
        and hit.confidence == "exact"
        for hit in attributes
    )
    calls = [hit for hit in chain_result.hits if hit.file == "app/chains.py" and hit.kind == "call"]
    assert any(
        hit.symbol == "requests.sessions.Session" and hit.line == 4 and hit.confidence == "possible"
        for hit in calls
    )

    re_export = trace(["requests.sessions.Session"])
    fallback_hits = [
        hit for hit in re_export.hits if hit.file == "app/re_export.py" and hit.kind == "call"
    ]
    assert len(fallback_hits) == 1
    assert fallback_hits[0].symbol == "requests.Session"
    assert fallback_hits[0].confidence == "possible"


def test_call_metadata_and_keywords() -> None:
    result = trace(["requests.sessions.Session"])

    calls = sorted(
        [hit for hit in result.hits if hit.file == "app/calls.py" and hit.kind == "call"],
        key=lambda hit: hit.line,
    )
    assert calls[0].keywords == ("timeout", "verify")
    assert calls[0].n_positional == 0
    assert not calls[0].has_star_args
    assert not calls[0].has_star_kwargs

    assert calls[1].keywords == ()
    assert calls[1].n_positional == 1
    assert not calls[1].has_star_args

    assert calls[2].has_star_kwargs
    assert calls[2].n_positional == 0

    assert calls[3].has_star_args
    assert calls[3].n_positional == 1


def test_parameters_are_echoed_without_changing_confidence() -> None:
    result = trace(
        ["requests.sessions.Session"],
        parameters={
            "requests.sessions.Session": [
                ("timeout", "PARAMETER_REMOVED"),
                ("timeout", "PARAMETER_REMOVED"),
            ]
        },
    )

    assert result.parameters == {"requests.sessions.Session": [("timeout", "PARAMETER_REMOVED")]}
    first_call = next(
        hit
        for hit in result.hits
        if hit.file == "app/calls.py" and hit.kind == "call" and hit.line == 3
    )
    assert first_call.confidence == "exact"


def test_star_import_risk_and_possible_hit() -> None:
    result = trace(["requests.sessions.Session"])

    assert "star_import" in result.risks
    calls = [hit for hit in result.hits if hit.file == "app/star.py" and hit.kind == "call"]
    assert calls
    assert calls[0].confidence == "possible"
    imports = [hit for hit in result.hits if hit.file == "app/star.py" and hit.kind == "import"]
    assert imports
    assert imports[0].confidence == "possible"


def test_getattr_literal_and_dynamic() -> None:
    result = trace(["requests.sessions.Session"])

    hits = [
        hit
        for hit in result.hits
        if hit.file == "app/getattr_case.py" and hit.symbol == "requests.sessions.Session"
    ]
    assert any(hit.line == 3 and hit.kind == "attribute" for hit in hits)
    assert "dynamic_getattr" in result.risks


def test_dynamic_import_risks() -> None:
    result = trace(["requests.sessions.Session"])

    assert "dynamic_import" in result.risks
    assert "dynamic_import_unknown_target" in result.risks


def test_unrelated_dynamic_import_literal_has_no_risk(tmp_path: Path) -> None:
    write_python(
        tmp_path,
        "main.py",
        "import importlib\nimportlib.import_module('json')\n",
    )

    result = trace_usage(tmp_path, "requests", ["requests.sessions.Session"])

    assert "dynamic_import" not in result.risks
    assert "dynamic_import_unknown_target" not in result.risks


def test_type_checking_import_is_recorded() -> None:
    result = trace(["requests.sessions.Session"])

    assert any(
        hit.file == "app/type_checking.py"
        and hit.kind == "import"
        and hit.symbol == "requests.sessions.Session"
        for hit in result.hits
    )
    assert any("TYPE_CHECKING" in warning for warning in result.warnings)


def test_syntax_error_file_is_skipped() -> None:
    result = trace(["requests.sessions.Session"])

    assert SkippedFile(file="app/broken.py", reason="unparsable") in result.files_skipped
    assert "unparsable_file" in result.risks
    assert any("broken.py" in warning for warning in result.warnings)


def test_venv_and_other_excluded_dirs_are_skipped(tmp_path: Path) -> None:
    write_python(
        tmp_path,
        "main.py",
        "from requests.sessions import Session\nSession()\n",
    )
    for directory in (
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        "site-packages",
    ):
        write_python(
            tmp_path / directory,
            "ignored.py",
            "from requests.sessions import Session\nSession()\n",
        )

    result = trace_usage(tmp_path, "requests", ["requests.sessions.Session"])

    assert result.files_scanned == 1
    assert result.files_skipped == []
    assert {hit.file for hit in result.hits} == {"main.py"}


def test_package_own_source_requires_matching_project_name(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_python(
        repo / "src" / "mypkg",
        "mod.py",
        "from mypkg import thing\nthing()\n",
    )
    pyproject = repo / "pyproject.toml"
    pyproject.write_text('[project]\nname = "other"\n', encoding="utf-8")

    scanned = trace_usage(repo, "mypkg", ["mypkg.thing"])
    assert scanned.files_scanned == 1
    assert any(hit.symbol == "mypkg.thing" and hit.kind == "call" for hit in scanned.hits)

    pyproject.write_text('[project]\nname = "mypkg"\n', encoding="utf-8")
    skipped = trace_usage(repo, "mypkg", ["mypkg.thing"])
    assert skipped.files_scanned == 0
    assert skipped.hits == []


def test_default_import_name_replaces_dashes(tmp_path: Path) -> None:
    write_python(
        tmp_path,
        "main.py",
        "from my_pkg import thing\nthing()\n",
    )

    result = trace_usage(tmp_path, "my-pkg", ["my_pkg.thing"])

    assert any(hit.symbol == "my_pkg.thing" and hit.kind == "call" for hit in result.hits)


def test_same_scope_instance_tracking_only(tmp_path: Path) -> None:
    result = trace(["requests.sessions.Session.request"])

    requests_hits = [
        hit
        for hit in result.hits
        if hit.file == "app/instances.py" and hit.symbol == "requests.sessions.Session.request"
    ]
    assert {hit.line for hit in requests_hits} == {4, 9}
    assert all(hit.confidence == "possible" for hit in requests_hits)
    assert 13 not in {hit.line for hit in requests_hits}


def test_method_target_untraced_risk(tmp_path: Path) -> None:
    write_python(tmp_path, "main.py", "value = 1\n")

    untraced = trace_usage(tmp_path, "pkg", ["pkg.Foo.bar"])
    assert "method_target_untraced" in untraced.risks

    plain = trace_usage(tmp_path, "pkg", ["pkg.foo"])
    assert "method_target_untraced" not in plain.risks

    traced = trace(["requests.sessions.Session.request"])
    assert "method_target_untraced" not in traced.risks


def test_results_are_deterministic_and_sorted() -> None:
    first = trace(["requests.sessions.Session.request", "requests.sessions.Session"])
    second = trace(["requests.sessions.Session", "requests.sessions.Session.request"])

    assert first == second
    keys = [(hit.file, hit.line, hit.col, hit.symbol, hit.kind) for hit in first.hits]
    assert keys == sorted(keys)
    assert first.symbols == sorted(first.symbols)
    assert first.risks == sorted(first.risks)
    assert all(len(hit.snippet) <= 200 for hit in first.hits)


def test_file_cap_adds_scan_truncated_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_module, "_MAX_FILES", 1)

    result = trace(["requests.sessions.Session"])

    assert "scan_truncated" in result.risks
    assert any("File cap" in warning for warning in result.warnings)
    assert result.files_scanned <= 1


def test_large_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_python(
        tmp_path,
        "main.py",
        "from requests.sessions import Session\nSession()\n",
    )
    monkeypatch.setattr(usage_module, "_MAX_FILE_BYTES", 10)

    result = trace_usage(tmp_path, "requests", ["requests.sessions.Session"])

    assert SkippedFile(file="main.py", reason="too_large") in result.files_skipped
    assert result.files_scanned == 0


def test_symlink_file_is_not_followed(tmp_path: Path) -> None:
    target = write_python(
        tmp_path,
        "real.py",
        "from requests.sessions import Session\nSession()\n",
    )
    link = tmp_path / "link.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks are not available on this platform")

    result = trace_usage(tmp_path, "requests", ["requests.sessions.Session"])

    assert SkippedFile(file="link.py", reason="symlink") in result.files_skipped
    assert {hit.file for hit in result.hits} == {"real.py"}


def test_cli_usage_prints_json() -> None:
    result = CliRunner().invoke(
        app,
        ["usage", str(FIXTURE), "requests", "--symbols", "requests.sessions.Session"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["package"] == "requests"
    assert payload["symbols"] == ["requests.sessions.Session"]
    assert payload["hits"]
    assert payload["files_scanned"] > 0


def test_cli_usage_accepts_repeated_symbols() -> None:
    result = CliRunner().invoke(
        app,
        [
            "usage",
            str(FIXTURE),
            "requests",
            "--symbols",
            "requests.sessions.Session",
            "--symbols",
            "requests.sessions.Session.request",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["symbols"] == [
        "requests.sessions.Session",
        "requests.sessions.Session.request",
    ]


def test_cli_usage_uses_api_diff_symbols_and_parameters(tmp_path: Path) -> None:
    diff = ApiDiffResult(
        package="requests",
        old_version="1.0.0",
        new_version="2.0.0",
        status="ok",
        top_level=["requests"],
        changes=[
            ApiChange(
                kind="PARAMETER_REMOVED",
                symbol="requests.sessions.Session",
                severity="high",
                detail="Removed parameter",
                parameter="timeout",
            )
        ],
    )
    api_diff = tmp_path / "diff.json"
    api_diff.write_text(diff.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["usage", str(FIXTURE), "requests", "--api-diff", str(api_diff)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["symbols"] == ["requests.sessions.Session"]
    assert payload["parameters"] == {
        "requests.sessions.Session": [["timeout", "PARAMETER_REMOVED"]]
    }
    assert any(
        hit["line"] == 3 and "timeout" in hit["keywords"]
        for hit in payload["hits"]
        if hit["file"] == "app/calls.py" and hit["kind"] == "call"
    )


def test_cli_usage_import_names_come_from_api_diff_top_level(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_python(repo, "main.py", "from alpha import thing\nthing()\n")
    diff = ApiDiffResult(
        package="alpha",
        old_version="1.0.0",
        new_version="2.0.0",
        status="ok",
        top_level=["alpha", "beta"],
        changes=[
            ApiChange(
                kind="OBJECT_REMOVED",
                symbol="alpha.sub.thing",
                severity="high",
                detail="Removed",
            )
        ],
    )
    api_diff = tmp_path / "diff.json"
    api_diff.write_text(diff.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["usage", str(repo), "alpha", "--api-diff", str(api_diff)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert any(
        hit["symbol"] == "alpha.thing" and hit["confidence"] == "possible"
        for hit in payload["hits"]
    )

    overridden = CliRunner().invoke(
        app,
        [
            "usage",
            str(repo),
            "alpha",
            "--api-diff",
            str(api_diff),
            "--import-name",
            "gamma",
        ],
    )

    assert overridden.exit_code == 0
    overridden_payload = json.loads(overridden.output)
    assert not any(hit["symbol"] == "alpha.thing" for hit in overridden_payload["hits"])


def test_cli_usage_bad_repo_path_exits_2(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "usage",
            str(tmp_path / "missing"),
            "requests",
            "--symbols",
            "requests.sessions.Session",
        ],
    )

    assert result.exit_code == 2


def test_cli_usage_requires_symbols_or_api_diff() -> None:
    result = CliRunner().invoke(app, ["usage", str(FIXTURE), "requests"])

    assert result.exit_code == 2


def test_cli_usage_rejects_invalid_api_diff(tmp_path: Path) -> None:
    api_diff = tmp_path / "diff.json"
    api_diff.write_text("{not json", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["usage", str(FIXTURE), "requests", "--api-diff", str(api_diff)],
    )

    assert result.exit_code == 2


def test_cli_usage_accepts_api_diff_with_utf8_bom(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_python(repo, "main.py", "from alpha import thing\nthing()\n")
    diff = ApiDiffResult(
        package="alpha",
        old_version="1.0.0",
        new_version="2.0.0",
        status="ok",
        top_level=["alpha"],
        changes=[
            ApiChange(
                kind="OBJECT_REMOVED",
                symbol="alpha.thing",
                severity="high",
                detail="Removed",
            )
        ],
    )
    api_diff = tmp_path / "diff.json"
    api_diff.write_text("\ufeff" + diff.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["usage", str(repo), "alpha", "--api-diff", str(api_diff)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert any(hit["symbol"] == "alpha.thing" for hit in payload["hits"])
