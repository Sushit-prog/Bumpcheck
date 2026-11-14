from __future__ import annotations

import importlib.util
import sys
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
