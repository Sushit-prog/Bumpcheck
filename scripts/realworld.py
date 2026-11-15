"""Fetch real-world lockfile changes from GitHub PRs and validate the parser.

Dev-only helper; it is not part of the installed package and uses only the
standard library for networking.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name

from bumpcheck.lockdiff import LockParseError, parse_lock_pair

ROOT = Path(__file__).resolve().parents[1]
PRS_FILE = ROOT / "realworld" / "prs.txt"
DATA_DIR = ROOT / "realworld" / "data"
REPORT_MD = ROOT / "realworld" / "report.md"
REPORT_JSON = ROOT / "realworld" / "report.json"

API_ROOT = "https://api.github.com"
RAW_ROOT = "https://raw.githubusercontent.com"
USER_AGENT = "bumpcheck-realworld/0.1"
MAX_FILES = 300
PAGE_SIZE = 100
REQUEST_SLEEP = 0.5
REQUIREMENTS_GLOB = "requirements*.txt"
TOP_EXTRA_BUMPS = 10

_TITLE_RE = re.compile(
    r"^(?:(?P<prefix>[A-Za-z]+)\(deps[^)]*\):?\s+)?"
    r"[Bb]ump\s+(?P<name>\S+)\s+from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+)"
    r"(?:\s+in\s+(?P<path>\S+))?\s*$"
)


@dataclass(frozen=True)
class DependabotTitle:
    name: str
    old_version: str
    new_version: str
    path: str | None = None


class RateLimited(Exception):
    """Raised when GitHub asks us to stop making requests."""

    def __init__(self, reset: str | None, retry_after: str | None) -> None:
        self.reset = reset
        self.retry_after = retry_after
        super().__init__("GitHub rate limit reached")


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """Parse a GitHub pull request URL into (owner, repo, number)."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"not a GitHub URL: {url!r}")
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError(f"not a GitHub URL: {url!r}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "pull":
        raise ValueError(f"not a GitHub pull request URL: {url!r}")
    owner, repo, number_text = parts[0], parts[1], parts[3]
    if not number_text.isdigit():
        raise ValueError(f"invalid pull request number in {url!r}")
    return owner, repo, int(number_text)


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def lockfile_kind(path: str) -> str | None:
    """Return the parser kind for a repo path, or None if it is not a lockfile."""
    basename = _basename(path)
    if basename == "uv.lock":
        return "uv.lock"
    if basename == "poetry.lock":
        return "poetry.lock"
    if fnmatch.fnmatchcase(basename, REQUIREMENTS_GLOB):
        return "requirements"
    return None


def select_lockfiles(files: list[dict]) -> list[dict]:
    """Keep only the changed files that are lockfiles, preserving order."""
    return [entry for entry in files if lockfile_kind(str(entry.get("filename", ""))) is not None]


def parse_dependabot_title(title: str) -> DependabotTitle | None:
    """Parse a Dependabot bump title, or return None for other titles."""
    match = _TITLE_RE.match(title.strip())
    if match is None:
        return None
    return DependabotTitle(
        name=match.group("name"),
        old_version=match.group("old"),
        new_version=match.group("new"),
        path=match.group("path"),
    )


def _headers(token: str | None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(url: str, token: str | None) -> bytes:
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        remaining = exc.headers.get("X-RateLimit-Remaining")
        retry_after = exc.headers.get("Retry-After")
        if exc.code in {403, 429} and (remaining == "0" or retry_after is not None):
            raise RateLimited(exc.headers.get("X-RateLimit-Reset"), retry_after) from exc
        raise
    finally:
        time.sleep(REQUEST_SLEEP)


def _get_json(url: str, token: str | None) -> object:
    return json.loads(_get(url, token).decode("utf-8"))


def _download(repo_full_name: str, path: str, sha: str, token: str | None) -> str:
    owner, _, repo = repo_full_name.partition("/")
    encoded = urllib.parse.quote(path, safe="/")
    return _get(f"{RAW_ROOT}/{owner}/{repo}/{sha}/{encoded}", token).decode("utf-8")


def _read_pr_urls() -> list[str]:
    if not PRS_FILE.exists():
        raise SystemExit(f"{PRS_FILE} does not exist; add GitHub PR URLs to it first.")
    urls = []
    for line in PRS_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            urls.append(stripped)
    return urls


def _fetch_files(owner: str, repo: str, number: int, token: str | None) -> list[dict]:
    files: list[dict] = []
    page = 1
    while len(files) < MAX_FILES:
        url = (
            f"{API_ROOT}/repos/{owner}/{repo}/pulls/{number}/files?per_page={PAGE_SIZE}&page={page}"
        )
        chunk = _get_json(url, token)
        if not isinstance(chunk, list) or not chunk:
            break
        files.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            break
        page += 1
    return files[:MAX_FILES]


def _fetch_side(
    repo_full_name: str,
    path: str,
    sha: str,
    kind: str,
    token: str | None,
) -> tuple[str | None, str | None]:
    """Return (text, note); text is None when the side must be skipped."""
    try:
        return _download(repo_full_name, path, sha, token), None
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            return None, f"HTTP {exc.code} fetching {path}"
        if kind == "requirements":
            return "", None
        return None, f"{path} missing on one side (skipped for {kind})"


def _format_exception(exc: BaseException) -> str:
    """Format an exception as "<Type>: <message>", including OSError filenames."""
    message = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, OSError) and exc.filename and str(exc.filename) not in str(exc):
        message = f"{message}: {exc.filename}"
    return message


def _fetch_one(url: str, token: str | None, force: bool, verbose: bool = False) -> str:
    try:
        owner, repo, number = parse_pr_url(url)
    except ValueError as exc:
        return f"skip {url}: {exc}"

    pr_dir = DATA_DIR / f"{owner}__{repo}__{number}"
    if (pr_dir / "meta.json").exists() and not force:
        return f"skip {owner}/{repo}#{number}: already fetched"

    pr_dir.mkdir(parents=True, exist_ok=True)

    try:
        pr = _get_json(f"{API_ROOT}/repos/{owner}/{repo}/pulls/{number}", token)
        assert isinstance(pr, dict)
        base_sha = pr["base"]["sha"]
        head_sha = pr["head"]["sha"]
        base_repo = (pr.get("base") or {}).get("repo") or {}
        base_repo = base_repo.get("full_name") or f"{owner}/{repo}"
        head_repo = (pr.get("head") or {}).get("repo") or {}
        head_repo = head_repo.get("full_name") or base_repo

        selected = select_lockfiles(_fetch_files(owner, repo, number, token))
        meta = {
            "url": url,
            "owner": owner,
            "repo": repo,
            "number": number,
            "title": pr.get("title", ""),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "base_repo": base_repo,
            "head_repo": head_repo,
            "files": [],
            "notes": [] if selected else ["no lockfiles changed"],
        }

        for index, entry in enumerate(selected):
            new_path = str(entry.get("filename", ""))
            kind = lockfile_kind(new_path)
            if kind is None:
                continue
            if entry.get("status") == "renamed":
                old_path = str(entry.get("previous_filename") or new_path)
            else:
                old_path = new_path

            old_text, old_note = _fetch_side(base_repo, old_path, base_sha, kind, token)
            new_text, new_note = _fetch_side(head_repo, new_path, head_sha, kind, token)
            note = old_note or new_note
            if old_text is None or new_text is None or note is not None:
                meta["notes"].append(f"{new_path}: {note or 'skipped'}")
                continue

            (pr_dir / f"{index}.old").write_text(old_text, encoding="utf-8")
            (pr_dir / f"{index}.new").write_text(new_text, encoding="utf-8")
            meta["files"].append(
                {
                    "index": index,
                    "path": new_path,
                    "old_path": old_path,
                    "new_path": new_path,
                    "kind": kind,
                }
            )
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad PR must not stop the run
        if verbose:
            traceback.print_exc()
        error = {
            "url": url,
            "owner": owner,
            "repo": repo,
            "number": number,
            "error": _format_exception(exc),
        }
        (pr_dir / "error.json").write_text(json.dumps(error, indent=2) + "\n", encoding="utf-8")
        return f"error {owner}/{repo}#{number}: {_format_exception(exc)}"

    stale_error = pr_dir / "error.json"
    if stale_error.exists():
        stale_error.unlink()
    (pr_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return f"fetched {owner}/{repo}#{number}: {len(meta['files'])} lockfile(s)"


def _print_rate_limit(exc: RateLimited) -> None:
    print("GitHub rate limit reached; stopping fetch.")
    if exc.reset and exc.reset.isdigit():
        reset = datetime.datetime.fromtimestamp(int(exc.reset), tz=datetime.timezone.utc)
        print(f"Rate limit resets at {reset.isoformat()} (X-RateLimit-Reset={exc.reset}).")
    if exc.retry_after:
        print(f"Retry-After: {exc.retry_after}s.")
    print("Rerun `fetch` later to resume; already-fetched PRs are skipped.")


def fetch(force: bool, verbose: bool = False) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set; using unauthenticated GitHub API (lower rate limits).")
    urls = _read_pr_urls()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for url in urls:
        try:
            print(_fetch_one(url, token, force, verbose))
        except RateLimited as exc:
            _print_rate_limit(exc)
            return 3
    return 0


def _load_metas() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    metas = []
    for meta_path in sorted(DATA_DIR.glob("*/meta.json")):
        metas.append(json.loads(meta_path.read_text(encoding="utf-8")))
    return metas


def _load_fetch_errors() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    errors = []
    for error_path in sorted(DATA_DIR.glob("*/error.json")):
        if (error_path.parent / "meta.json").exists():
            continue
        errors.append(json.loads(error_path.read_text(encoding="utf-8")))
    return errors


def _analyse_file(pr_dir: Path, entry: dict) -> dict:
    kind = entry["kind"]
    old_text = (pr_dir / f"{entry['index']}.old").read_text(encoding="utf-8")
    new_text = (pr_dir / f"{entry['index']}.new").read_text(encoding="utf-8")
    try:
        result = parse_lock_pair(old_text, new_text, kind)
    except LockParseError as exc:
        return {
            "status": "lock_parse_error",
            "exception": type(exc).__name__,
            "message": str(exc),
            "bumps": [],
            "warnings": [],
        }
    except Exception as exc:  # noqa: BLE001 - report unexpected failures
        return {
            "status": "unexpected_exception",
            "exception": type(exc).__name__,
            "message": str(exc),
            "bumps": [],
            "warnings": [],
        }
    return {
        "status": "ok",
        "exception": None,
        "message": None,
        "bumps": [bump.model_dump() for bump in result.bumps],
        "warnings": list(result.warnings),
    }


def _cross_check(title: str, file_reports: list[dict]) -> tuple[str, int | None]:
    parsed = parse_dependabot_title(title)
    if parsed is None:
        return "NOT_APPLICABLE", None
    canonical = canonicalize_name(parsed.name)
    bumps = [bump for report in file_reports for bump in report["bumps"]]
    matched = any(
        bump["package"] == canonical
        and bump["old_version"] == parsed.old_version
        and bump["new_version"] == parsed.new_version
        for bump in bumps
    )
    if matched:
        return "MATCH", len(bumps) - 1
    return "MISMATCH", None


def build_report() -> dict:
    summary = {
        "prs": 0,
        "files": 0,
        "parsed_ok": 0,
        "lock_parse_errors": 0,
        "unexpected_exceptions": 0,
        "files_with_warnings": 0,
        "fetch_errors": 0,
    }
    prs: list[dict] = []
    mismatches: list[dict] = []

    for meta in _load_metas():
        summary["prs"] += 1
        pr_dir = DATA_DIR / f"{meta['owner']}__{meta['repo']}__{meta['number']}"
        file_reports = []
        for entry in meta.get("files", []):
            report = _analyse_file(pr_dir, entry)
            report.update({"index": entry["index"], "path": entry["path"], "kind": entry["kind"]})
            summary["files"] += 1
            if report["status"] == "ok":
                summary["parsed_ok"] += 1
            elif report["status"] == "lock_parse_error":
                summary["lock_parse_errors"] += 1
            else:
                summary["unexpected_exceptions"] += 1
            if report["warnings"]:
                summary["files_with_warnings"] += 1
            file_reports.append(report)

        status, extra_bumps = _cross_check(meta.get("title", ""), file_reports)
        pr_report = {
            "url": meta.get("url"),
            "title": meta.get("title", ""),
            "status": status,
            "extra_bumps": extra_bumps,
            "notes": meta.get("notes", []),
            "files": file_reports,
        }
        prs.append(pr_report)
        if status == "MISMATCH":
            mismatches.append(
                {
                    "url": meta.get("url"),
                    "title": meta.get("title", ""),
                    "bumps": [bump for report in file_reports for bump in report["bumps"]],
                }
            )

    fetch_errors = _load_fetch_errors()
    summary["fetch_errors"] = len(fetch_errors)

    matched = [pr for pr in prs if pr["status"] == "MATCH"]
    top_extra = sorted(
        (pr for pr in matched if (pr["extra_bumps"] or 0) > 0),
        key=lambda pr: pr["extra_bumps"] or 0,
        reverse=True,
    )[:TOP_EXTRA_BUMPS]
    extra_bumps_top = [
        {"url": pr["url"], "title": pr["title"], "extra_bumps": pr["extra_bumps"]}
        for pr in top_extra
    ]

    return {
        "summary": summary,
        "mismatches": mismatches,
        "extra_bumps_top": extra_bumps_top,
        "fetch_errors": fetch_errors,
        "prs": prs,
    }


def _render_markdown(report: dict) -> str:
    lines = ["# BumpCheck real-world validation", ""]
    lines.append("## Title mismatches")
    lines.append("")
    if report["mismatches"]:
        for item in report["mismatches"]:
            lines.append(f"- {item['title']} ({item['url']})")
            lines.append(f"  - parsed bumps: {json.dumps(item['bumps'])}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Fetch errors")
    lines.append("")
    if report["fetch_errors"]:
        for item in report["fetch_errors"]:
            lines.append(f"- {item.get('url')}: {item.get('error')}")
    else:
        lines.append("- none")
    lines.append("")

    summary = report["summary"]
    lines.append("## Summary")
    lines.append("")
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.append("")

    lines.append("## Highest extra bumps")
    lines.append("")
    if report["extra_bumps_top"]:
        for item in report["extra_bumps_top"]:
            lines.append(f"- {item['extra_bumps']}: {item['title']} ({item['url']})")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Pull requests")
    lines.append("")
    for pr in report["prs"]:
        lines.append(f"### {pr['title']}")
        lines.append("")
        lines.append(f"- url: {pr['url']}")
        lines.append(f"- title status: {pr['status']}")
        if pr["extra_bumps"] is not None:
            lines.append(f"- extra bumps: {pr['extra_bumps']}")
        for note in pr["notes"]:
            lines.append(f"- note: {note}")
        for file_report in pr["files"]:
            lines.append(
                f"- file {file_report['path']} ({file_report['kind']}): "
                f"{file_report['status']}, {len(file_report['bumps'])} bump(s), "
                f"{len(file_report['warnings'])} warning(s)"
            )
            if file_report["message"]:
                lines.append(f"  - {file_report['exception']}: {file_report['message']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def report() -> int:
    data = build_report()
    REPORT_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    REPORT_MD.write_text(_render_markdown(data), encoding="utf-8")
    summary = data["summary"]
    print(
        f"report written: {summary['prs']} PR(s), {summary['files']} file(s), "
        f"{summary['parsed_ok']} ok, {summary['lock_parse_errors']} parse error(s), "
        f"{summary['unexpected_exceptions']} unexpected, "
        f"{summary['fetch_errors']} fetch error(s)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch_parser = subparsers.add_parser("fetch", help="download PR lockfiles")
    fetch_parser.add_argument("--force", action="store_true", help="refetch existing PRs")
    fetch_parser.add_argument(
        "--verbose",
        action="store_true",
        help="print the full traceback for each failed PR",
    )
    subparsers.add_parser("report", help="build report.md and report.json from fetched data")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return fetch(force=args.force, verbose=args.verbose)
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
