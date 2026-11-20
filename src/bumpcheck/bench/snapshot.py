"""Offline snapshot store for benchmark cases."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

from bumpcheck import archive
from bumpcheck.apidiff import _PYPI_JSON_URL, _choose_wheel
from bumpcheck.bench.github import DownloadTooLarge, GitHubClient, GitHubError

MAX_EXTRACTED_BYTES = archive.MAX_EXTRACTED_BYTES
TERMINAL_STATUSES = frozenset({"complete", "no_wheel", "snapshot_too_large", "snapshot_incomplete"})


def case_slug(candidate: dict[str, Any]) -> str:
    repo = str(candidate.get("repo") or "unknown/unknown")
    owner, _, name = repo.partition("/")
    number = candidate.get("pr_number", 0)

    def clean(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or "unknown"

    return f"{clean(owner)}__{clean(name)}__{clean(str(number))}"


def snapshot_path(root: Path, candidate: dict[str, Any]) -> Path:
    return Path(root) / case_slug(candidate)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _pypi_url(name: str, version: str) -> str:
    return _PYPI_JSON_URL.format(
        name=urllib.parse.quote(name, safe=""),
        version=urllib.parse.quote(version, safe=""),
    )


def _metadata_wheel(payload: dict[str, Any], name: str, version: str) -> dict[str, Any] | None:
    try:
        return _choose_wheel(list(payload.get("urls") or []), name, version)
    except Exception:
        return None


def _finalize(staging: Path, target: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    _write_manifest(target / "manifest.json", manifest)
    return manifest


def load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def should_snapshot(manifest: dict[str, Any] | None, *, force: bool) -> bool:
    if force or manifest is None:
        return True
    return str(manifest.get("status") or "failed") not in TERMINAL_STATUSES


def directory_size(path: Path) -> int:
    return archive.directory_size(path)


def snapshot_case(
    candidate: dict[str, Any],
    root: Path,
    *,
    github_client: GitHubClient,
    force: bool = False,
) -> dict[str, Any]:
    case_id = str(candidate.get("case_id") or "")
    target = snapshot_path(root, candidate)
    manifest_path = target / "manifest.json"
    existing = load_manifest(manifest_path)
    if not should_snapshot(existing, force=force):
        assert existing is not None
        return existing

    slug = case_slug(candidate)
    staging = target.parent / f".{slug}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    package = str(candidate.get("package") or "")
    old_version = candidate.get("old_version")
    new_version = candidate.get("new_version")
    manifest: dict[str, Any] = {
        "case_id": case_id,
        "slug": slug,
        "status": "failed",
        "candidate": {
            "repo": candidate.get("repo"),
            "pr_number": candidate.get("pr_number"),
            "package": package,
            "old_version": old_version,
            "new_version": new_version,
            "base_sha": candidate.get("base_sha"),
            "head_sha": candidate.get("head_sha"),
            "lockfile_kind": candidate.get("lockfile_kind"),
            "lockfile_paths": candidate.get("lockfile_paths") or [],
        },
        "old": None,
        "new": None,
        "repo": None,
        "lockfiles": {},
        "skipped": {"files": 0, "bytes": 0, "reasons": {}},
    }

    try:
        if not package or not isinstance(old_version, str) or not isinstance(new_version, str):
            manifest["error"] = "candidate is missing package or comparable versions"
            return _finalize(staging, target, manifest)

        metadata_bytes: dict[str, bytes] = {}
        wheels: dict[str, dict[str, Any]] = {}
        for label, version in (("old", old_version), ("new", new_version)):
            url = _pypi_url(package, version)
            raw = github_client.get_external_bytes_limited(url)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"invalid PyPI metadata for {package} {version}")
            entry = _metadata_wheel(payload, package, version)
            if entry is None:
                manifest["status"] = "no_wheel"
                manifest["error"] = f"no wheel for {package} {version}"
                return _finalize(staging, target, manifest)
            metadata_bytes[label] = raw
            wheels[label] = entry

        extraction_stats: dict[str, archive.ExtractionStats] = {}
        for label, version in (("old", old_version), ("new", new_version)):
            entry = wheels[label]
            wheel_url = str(entry["url"])
            wheel_bytes = github_client.get_external_bytes_limited(wheel_url)
            expected = str((entry.get("digests") or {}).get("sha256") or "")
            if expected and _sha256(wheel_bytes) != expected:
                raise ValueError(f"sha256 mismatch for {entry.get('filename')}")
            filename = str(entry["filename"])
            wheel_file = f"{label}/{filename}"
            _write(staging / wheel_file, wheel_bytes)
            _write(staging / f"{label}/metadata.json", metadata_bytes[label])
            wheel_stats = archive.extract_zip(
                wheel_bytes,
                staging / f".validate-{label}",
                selector=archive.select_wheel_member,
                cap_bytes=MAX_EXTRACTED_BYTES,
            )
            shutil.rmtree(staging / f".validate-{label}", ignore_errors=True)
            extraction_stats[label] = wheel_stats
            manifest[label] = {
                "version": version,
                "metadata_url": _pypi_url(package, version),
                "metadata_file": f"{label}/metadata.json",
                "wheel_url": wheel_url,
                "wheel_file": wheel_file,
                "wheel_filename": filename,
                "sha256": _sha256(wheel_bytes),
                "extraction": wheel_stats.to_dict(),
            }

        repo = str(candidate.get("repo") or "")
        base_sha = str(candidate.get("base_sha") or "")
        owner, _, name = repo.partition("/")
        if not owner or not name or not base_sha:
            raise ValueError("candidate is missing base repository metadata")
        repo_url = f"https://api.github.com/repos/{owner}/{name}/tarball/{base_sha}"
        repo_bytes = github_client.download_bytes(repo_url)
        repo_stats = archive.extract_tar(
            repo_bytes,
            staging / "repo",
            selector=archive.select_repo_member,
            cap_bytes=MAX_EXTRACTED_BYTES,
            strip_root=True,
        )
        extraction_stats["repo"] = repo_stats
        manifest["repo"] = {
            "url": repo_url,
            "sha": base_sha,
            "extraction": repo_stats.to_dict(),
        }

        for lock_path in candidate.get("lockfile_paths") or []:
            if not isinstance(lock_path, str) or not lock_path:
                continue
            encoded = urllib.parse.quote(lock_path, safe="/")
            old_url = f"https://raw.githubusercontent.com/{owner}/{name}/{base_sha}/{encoded}"
            new_url = (
                f"https://raw.githubusercontent.com/{owner}/{name}/"
                f"{candidate.get('head_sha')}/{encoded}"
            )
            old_text = github_client.get_text(old_url)
            new_text = github_client.get_text(new_url)
            old_file = f"old/lockfile-{len(manifest['lockfiles'])}.txt"
            new_file = f"new/lockfile-{len(manifest['lockfiles'])}.txt"
            _write(staging / old_file, old_text.encode("utf-8"))
            _write(staging / new_file, new_text.encode("utf-8"))
            manifest["lockfiles"][lock_path] = {"old": old_file, "new": new_file}

        skipped_files = sum(stats.skipped_count for stats in extraction_stats.values())
        skipped_bytes = sum(stats.skipped_bytes for stats in extraction_stats.values())
        reasons: dict[str, int] = {}
        for stats in extraction_stats.values():
            for item in stats.skipped:
                reasons[item.reason] = reasons.get(item.reason, 0) + 1
        manifest["skipped"] = {
            "files": skipped_files,
            "bytes": skipped_bytes,
            "reasons": reasons,
        }
        if any(stats.has_skipped_python for stats in extraction_stats.values()):
            manifest["status"] = "snapshot_incomplete"
            manifest["error"] = "a Python source file was skipped during extraction"
        else:
            manifest["status"] = "complete"
    except (DownloadTooLarge, archive.ArchiveError) as exc:
        if isinstance(exc, DownloadTooLarge) or getattr(exc, "kind", "") == "too_large":
            manifest["status"] = "snapshot_too_large"
        else:
            manifest["status"] = "failed"
        manifest["error"] = str(exc)
    except (GitHubError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"

    return _finalize(staging, target, manifest)
