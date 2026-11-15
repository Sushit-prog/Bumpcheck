from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "realworld.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("realworld_script", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


realworld = _load_module()


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bump requests from 2.31.0 to 2.32.0", ("requests", "2.31.0", "2.32.0", None)),
        ("bump Django from 4.2 to 5.0", ("Django", "4.2", "5.0", None)),
        (
            "build(deps): bump requests from 2.31.0 to 2.32.0",
            ("requests", "2.31.0", "2.32.0", None),
        ),
        (
            "chore(deps): bump requests from 2.31.0 to 2.32.0",
            ("requests", "2.31.0", "2.32.0", None),
        ),
        (
            "build(deps-dev): bump pytest from 8.0.0 to 8.1.0",
            ("pytest", "8.0.0", "8.1.0", None),
        ),
        (
            "chore(deps-dev) bump pytest from 8.0.0 to 8.1.0",
            ("pytest", "8.0.0", "8.1.0", None),
        ),
        (
            "Bump requests from 2.31.0 to 2.32.0 in /backend",
            ("requests", "2.31.0", "2.32.0", "/backend"),
        ),
        (
            "build(deps): bump requests from 2.31.0 to 2.32.0 in /services/api",
            ("requests", "2.31.0", "2.32.0", "/services/api"),
        ),
    ],
)
def test_parse_dependabot_title_matches(title: str, expected: tuple) -> None:
    parsed = realworld.parse_dependabot_title(title)
    assert parsed is not None
    assert (parsed.name, parsed.old_version, parsed.new_version, parsed.path) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Update requests",
        "Bump requests from 2.31.0 to 2.32.0 in the pip group across 1 directory",
        "Bump requests from 2.31.0 to 2.32.0 and 2.33.0",
        "updates: bump requests from 2.31.0 to 2.32.0",
        "Bump requests from 2.31.0",
    ],
)
def test_parse_dependabot_title_rejects(title: str) -> None:
    assert realworld.parse_dependabot_title(title) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("uv.lock", "uv.lock"),
        ("sub/dir/uv.lock", "uv.lock"),
        ("poetry.lock", "poetry.lock"),
        ("backend/poetry.lock", "poetry.lock"),
        ("requirements.txt", "requirements"),
        ("requirements-dev.txt", "requirements"),
        ("services/api/requirements-prod.txt", "requirements"),
        ("Requirements.txt", None),
        ("Poetry.lock", None),
        ("UV.LOCK", None),
        ("setup.py", None),
        ("uv.lock.bak", None),
        ("requirements.txt.bak", None),
    ],
)
def test_lockfile_kind(path: str, expected: str | None) -> None:
    assert realworld.lockfile_kind(path) == expected


def test_select_lockfiles_preserves_order() -> None:
    files = [
        {"filename": "README.md"},
        {"filename": "uv.lock"},
        {"filename": "requirements-dev.txt"},
        {"filename": "src/poetry.lock"},
    ]
    selected = realworld.select_lockfiles(files)
    assert [entry["filename"] for entry in selected] == [
        "uv.lock",
        "requirements-dev.txt",
        "src/poetry.lock",
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo/pull/123", ("owner", "repo", 123)),
        ("https://github.com/owner/repo/pull/123/", ("owner", "repo", 123)),
        ("https://github.com/owner/repo/pull/123/files", ("owner", "repo", 123)),
        ("https://github.com/owner/repo/pull/123/files/", ("owner", "repo", 123)),
        ("https://github.com/Owner/Repo/pull/123", ("Owner", "Repo", 123)),
        ("https://github.com/owner/repo/pull/123?diff=split", ("owner", "repo", 123)),
        ("http://github.com/owner/repo/pull/7", ("owner", "repo", 7)),
    ],
)
def test_parse_pr_url(url: str, expected: tuple) -> None:
    assert realworld.parse_pr_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo/pull/abc",
        "https://github.com/owner/repo/issues/1",
        "https://gitlab.com/owner/repo/pull/1",
        "not a url",
    ],
)
def test_parse_pr_url_rejects(url: str) -> None:
    with pytest.raises(ValueError):
        realworld.parse_pr_url(url)


RAW = "https://raw.githubusercontent.com"
API = "https://api.github.com"
PR_URL = "https://github.com/owner/repo/pull/1"
PR_API = f"{API}/repos/owner/repo/pulls/1"
FILES_API = f"{API}/repos/owner/repo/pulls/1/files?per_page=100&page=1"
BASE_SHA = "base123"
HEAD_SHA = "head456"

OLD_UV = 'version = 1\n\n[[package]]\nname = "packaging"\nversion = "24.0"\n'
NEW_UV = 'version = 1\n\n[[package]]\nname = "packaging"\nversion = "25.0"\n'
OLD_POETRY = '# poetry\n\n[[package]]\nname = "packaging"\nversion = "24.0"\n'
NEW_POETRY = '# poetry\n\n[[package]]\nname = "packaging"\nversion = "25.0"\n'


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _http_error(code: int, url: str, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, f"HTTP {code}", headers or {}, io.BytesIO(b""))


def _install_http(monkeypatch, routes: dict, errors: dict | None = None) -> None:
    errors = errors or {}

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        if url in errors:
            raise errors[url]
        if url in routes:
            return _FakeResponse(routes[url])
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(realworld.urllib.request, "urlopen", fake_urlopen)


def _setup(monkeypatch, tmp_path: Path, url: str = PR_URL) -> Path:
    data_dir = tmp_path / "data"
    prs_file = tmp_path / "prs.txt"
    prs_file.write_text(url + "\n", encoding="utf-8")
    monkeypatch.setattr(realworld, "DATA_DIR", data_dir)
    monkeypatch.setattr(realworld, "PRS_FILE", prs_file)
    monkeypatch.setattr(realworld, "REQUEST_SLEEP", 0)
    return data_dir


def _pr_payload(head_repo: str = "owner/repo") -> bytes:
    return _json_bytes(
        {
            "title": "Bump packaging from 24.0 to 25.0",
            "base": {"sha": BASE_SHA, "repo": {"full_name": "owner/repo"}},
            "head": {"sha": HEAD_SHA, "repo": {"full_name": head_repo}},
        }
    )


def test_fetch_creates_missing_data_dir_and_writes_files(monkeypatch, tmp_path: Path) -> None:
    data_dir = _setup(monkeypatch, tmp_path)
    _install_http(
        monkeypatch,
        {
            PR_API: _pr_payload(),
            FILES_API: _json_bytes([{"filename": "uv.lock", "status": "modified"}]),
            f"{RAW}/owner/repo/{BASE_SHA}/uv.lock": OLD_UV.encode("utf-8"),
            f"{RAW}/owner/repo/{HEAD_SHA}/uv.lock": NEW_UV.encode("utf-8"),
        },
    )

    assert realworld.fetch(force=False, verbose=False) == 0

    pr_dir = data_dir / "owner__repo__1"
    assert (pr_dir / "meta.json").exists()
    assert (pr_dir / "0.old").read_text(encoding="utf-8") == OLD_UV
    assert (pr_dir / "0.new").read_text(encoding="utf-8") == NEW_UV
    assert not (pr_dir / "error.json").exists()
    meta = json.loads((pr_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["files"] == [
        {
            "index": 0,
            "path": "uv.lock",
            "old_path": "uv.lock",
            "new_path": "uv.lock",
            "kind": "uv.lock",
        }
    ]


def test_fetch_renamed_uses_previous_filename_and_head_repo(monkeypatch, tmp_path: Path) -> None:
    data_dir = _setup(monkeypatch, tmp_path)
    _install_http(
        monkeypatch,
        {
            PR_API: _pr_payload(head_repo="contributor/repo"),
            FILES_API: _json_bytes(
                [
                    {
                        "filename": "poetry.lock",
                        "previous_filename": "old/poetry.lock",
                        "status": "renamed",
                    }
                ]
            ),
            f"{RAW}/owner/repo/{BASE_SHA}/old/poetry.lock": OLD_POETRY.encode("utf-8"),
            f"{RAW}/contributor/repo/{HEAD_SHA}/poetry.lock": NEW_POETRY.encode("utf-8"),
        },
    )

    assert realworld.fetch(force=False, verbose=False) == 0

    pr_dir = data_dir / "owner__repo__1"
    meta = json.loads((pr_dir / "meta.json").read_text(encoding="utf-8"))
    entry = meta["files"][0]
    assert entry["old_path"] == "old/poetry.lock"
    assert entry["new_path"] == "poetry.lock"
    assert entry["kind"] == "poetry.lock"
    assert (pr_dir / "0.old").read_text(encoding="utf-8") == OLD_POETRY
    assert (pr_dir / "0.new").read_text(encoding="utf-8") == NEW_POETRY


def test_fetch_handles_missing_side(monkeypatch, tmp_path: Path) -> None:
    data_dir = _setup(monkeypatch, tmp_path)
    routes = {
        PR_API: _pr_payload(),
        FILES_API: _json_bytes(
            [
                {"filename": "uv.lock", "status": "added"},
                {"filename": "requirements.txt", "status": "added"},
            ]
        ),
        f"{RAW}/owner/repo/{HEAD_SHA}/uv.lock": NEW_UV.encode("utf-8"),
        f"{RAW}/owner/repo/{HEAD_SHA}/requirements.txt": b"packaging==25.0\n",
    }
    errors = {
        f"{RAW}/owner/repo/{BASE_SHA}/uv.lock": _http_error(404, "uv.lock"),
        f"{RAW}/owner/repo/{BASE_SHA}/requirements.txt": _http_error(404, "requirements.txt"),
    }
    _install_http(monkeypatch, routes, errors)

    assert realworld.fetch(force=False, verbose=False) == 0

    pr_dir = data_dir / "owner__repo__1"
    meta = json.loads((pr_dir / "meta.json").read_text(encoding="utf-8"))
    assert [entry["path"] for entry in meta["files"]] == ["requirements.txt"]
    assert any("uv.lock" in note for note in meta["notes"])
    assert (pr_dir / "1.old").read_text(encoding="utf-8") == ""
    assert (pr_dir / "1.new").read_text(encoding="utf-8") == "packaging==25.0\n"


def test_fetch_rate_limit_stops_run_with_exit_3(monkeypatch, tmp_path: Path) -> None:
    data_dir = _setup(monkeypatch, tmp_path)
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"}
    _install_http(monkeypatch, {}, {PR_API: _http_error(403, PR_API, headers)})

    assert realworld.fetch(force=False, verbose=False) == 3

    assert not (data_dir / "owner__repo__1" / "meta.json").exists()
    assert not (data_dir / "owner__repo__1" / "error.json").exists()


def test_failed_pr_writes_error_json_only(monkeypatch, tmp_path: Path, capsys) -> None:
    data_dir = _setup(monkeypatch, tmp_path)
    _install_http(monkeypatch, {}, {PR_API: _http_error(500, PR_API)})

    assert realworld.fetch(force=False, verbose=False) == 0

    pr_dir = data_dir / "owner__repo__1"
    assert (pr_dir / "error.json").exists()
    assert not (pr_dir / "meta.json").exists()
    error = json.loads((pr_dir / "error.json").read_text(encoding="utf-8"))
    assert error["error"].startswith("HTTPError:")
    assert "500" in error["error"]
    out = capsys.readouterr().out
    assert "HTTPError:" in out
    assert "500" in out


def test_verbose_prints_traceback(monkeypatch, tmp_path: Path, capsys) -> None:
    _setup(monkeypatch, tmp_path)
    _install_http(monkeypatch, {}, {PR_API: _http_error(500, PR_API)})

    assert realworld.fetch(force=False, verbose=True) == 0

    assert "Traceback (most recent call last)" in capsys.readouterr().err


def test_successful_fetch_removes_stale_error_json(monkeypatch, tmp_path: Path) -> None:
    data_dir = _setup(monkeypatch, tmp_path)
    pr_dir = data_dir / "owner__repo__1"
    pr_dir.mkdir(parents=True)
    (pr_dir / "error.json").write_text('{"error": "stale"}\n', encoding="utf-8")
    _install_http(
        monkeypatch,
        {
            PR_API: _pr_payload(),
            FILES_API: _json_bytes([{"filename": "uv.lock", "status": "modified"}]),
            f"{RAW}/owner/repo/{BASE_SHA}/uv.lock": OLD_UV.encode("utf-8"),
            f"{RAW}/owner/repo/{HEAD_SHA}/uv.lock": NEW_UV.encode("utf-8"),
        },
    )

    assert realworld.fetch(force=False, verbose=False) == 0

    assert (pr_dir / "meta.json").exists()
    assert not (pr_dir / "error.json").exists()


def test_report_prefers_meta_over_stale_error_json(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    pr_dir = data_dir / "owner__repo__1"
    pr_dir.mkdir(parents=True)
    meta = {
        "url": PR_URL,
        "owner": "owner",
        "repo": "repo",
        "number": 1,
        "title": "Bump packaging from 24.0 to 25.0",
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "files": [],
        "notes": [],
    }
    (pr_dir / "meta.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    (pr_dir / "error.json").write_text('{"error": "stale"}\n', encoding="utf-8")
    monkeypatch.setattr(realworld, "DATA_DIR", data_dir)

    data = realworld.build_report()

    assert data["summary"]["prs"] == 1
    assert data["summary"]["fetch_errors"] == 0
    assert data["fetch_errors"] == []
