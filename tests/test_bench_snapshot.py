from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from bumpcheck.bench import snapshot


class FakeClient:
    def __init__(
        self,
        external: dict[str, bytes],
        downloads: dict[str, bytes],
        texts: dict[str, str],
    ) -> None:
        self.external = external
        self.downloads = downloads
        self.texts = texts

    def get_external_bytes_limited(self, url: str) -> bytes:
        return self.external[url]

    def download_bytes(self, url: str) -> bytes:
        return self.downloads[url]

    def get_text(self, url: str) -> str:
        return self.texts[url]


def wheel_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("pkg/__init__.py", "value = 1\n")
        handle.writestr("pkg-1.0.dist-info/METADATA", "Name: pkg\n")
    return buffer.getvalue()


def tar_bytes(names: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
        for name, content in names.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"repo-sha/{name}")
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def candidate() -> dict[str, Any]:
    return {
        "case_id": "owner/repo#1",
        "repo": "owner/repo",
        "pr_number": 1,
        "package": "pkg",
        "old_version": "1.0.0",
        "new_version": "2.0.0",
        "base_sha": "base",
        "head_sha": "head",
        "lockfile_kind": "requirements",
        "lockfile_paths": ["requirements.txt"],
        "split": "dev",
        "sampled_by": "failure",
    }


def fake_client(wheel: bytes | None = None, repo_names: dict[str, str] | None = None) -> FakeClient:
    wheel = wheel or wheel_bytes()
    repo_names = repo_names or {"main.py": "value = 1\n", "pyproject.toml": "[project]\n"}
    payload = {
        "info": {"name": "pkg"},
        "urls": [
            {
                "filename": "pkg-1.0.0-py3-none-any.whl",
                "url": "https://files.pythonhosted.org/pkg-1.0.0-py3-none-any.whl",
                "packagetype": "bdist_wheel",
                "digests": {"sha256": hashlib.sha256(wheel).hexdigest()},
            }
        ],
    }
    encoded = json.dumps(payload).encode("utf-8")
    external: dict[str, bytes] = {}
    for version in ("1.0.0", "2.0.0"):
        external[f"https://pypi.org/pypi/pkg/{version}/json"] = encoded
        external[f"https://files.pythonhosted.org/pkg-{version}-py3-none-any.whl"] = wheel
    repo_url = "https://api.github.com/repos/owner/repo/tarball/base"
    texts = {
        "https://raw.githubusercontent.com/owner/repo/base/requirements.txt": "pkg==1.0.0\n",
        "https://raw.githubusercontent.com/owner/repo/head/requirements.txt": "pkg==2.0.0\n",
    }
    return FakeClient(external, {repo_url: tar_bytes(repo_names)}, texts)


def test_snapshot_complete_and_resumable(tmp_path: Path) -> None:
    client = fake_client()
    root = tmp_path / "snapshots"
    item = candidate()

    manifest = snapshot.snapshot_case(item, root, github_client=client)
    assert manifest["status"] == "complete"
    case_dir = snapshot.snapshot_path(root, item)
    assert case_dir.name == "owner__repo__1"
    assert (case_dir / "manifest.json").is_file()
    assert (case_dir / "old" / "pkg-1.0.0-py3-none-any.whl").is_file()
    assert (case_dir / "repo" / "main.py").is_file()
    assert manifest["skipped"]["files"] == 0
    assert snapshot.directory_size(root) > 0

    client.external.clear()
    client.downloads.clear()
    again = snapshot.snapshot_case(item, root, github_client=client)
    assert again == manifest


def test_snapshot_no_wheel_is_terminal(tmp_path: Path) -> None:
    client = fake_client()
    client.external["https://pypi.org/pypi/pkg/2.0.0/json"] = json.dumps(
        {"info": {"name": "pkg"}, "urls": []}
    ).encode("utf-8")

    manifest = snapshot.snapshot_case(candidate(), tmp_path / "snapshots", github_client=client)

    assert manifest["status"] == "no_wheel"
    assert "no wheel" in manifest["error"]
    assert not list((tmp_path / "snapshots").glob(".*.staging"))


def test_snapshot_incomplete_when_python_file_skipped(tmp_path: Path) -> None:
    client = fake_client(repo_names={"bad:name.py": "value = 1\n", "main.py": "x = 1\n"})

    manifest = snapshot.snapshot_case(candidate(), tmp_path / "snapshots", github_client=client)

    assert manifest["status"] == "snapshot_incomplete"
    assert manifest["skipped"]["files"] >= 1
    assert "windows_invalid_name" in manifest["skipped"]["reasons"]


def test_snapshot_too_large_on_wheel_extraction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(snapshot, "MAX_EXTRACTED_BYTES", 5)
    client = fake_client()

    manifest = snapshot.snapshot_case(candidate(), tmp_path / "snapshots", github_client=client)

    assert manifest["status"] == "snapshot_too_large"


def test_failed_snapshots_are_retried() -> None:
    assert snapshot.should_snapshot({"status": "failed"}, force=False) is True
    assert snapshot.should_snapshot({"status": "no_wheel"}, force=False) is False
    assert snapshot.should_snapshot({"status": "complete"}, force=True) is True
