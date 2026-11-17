"""Mine public Dependabot and Renovate pull requests as benchmark candidates.

Candidates are hand-labelled later; the CI outcome only yields a *suggestion*
(``OK_SUSPECT`` / ``BROKE_SUSPECT`` / ``UNCLEAR``) and must be verified.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from packaging.utils import canonicalize_name

from bumpcheck.bench import github
from bumpcheck.bench.github import GitHubClient, GitHubError, HttpStatusError
from bumpcheck.bench.lockfiles import lockfile_kind
from bumpcheck.bench.models import (
    BaseComparisonStatus,
    Candidate,
    CheckRef,
    Excluded,
    ExcludedReason,
    FailureExcerpt,
    FailureVsBase,
    LaterFix,
    Summary,
)
from bumpcheck.bench.titles import Bot, classify_title, loose_version_match
from bumpcheck.lockdiff import LockParseError, parse_lock_pair

PAGE_SIZE = 100
MAX_FILES = 300
MAX_CHECK_PAGES = 10
MAX_WINDOW_DEPTH = 14
DEFAULT_SINCE_DAYS = 180
MAX_LOG_LINES_PER_CHECK = 4
MAX_LOG_LINES_PER_CASE = 40
MAX_LOG_BYTES_PER_CASE = 16 * 1024
MAX_LOG_LINE_LENGTH = 500
REPLACE_RETRIES = 3
REPLACE_RETRY_SLEEP = 0.2
MAX_LATER_FIXES = 5
LATER_FIX_CALL_BUDGET = 2
TEST_SPLIT_THRESHOLD = 77

FAILURE_CONCLUSIONS = {
    "failure",
    "timed_out",
    "action_required",
    "startup_failure",
    "error",
}
NON_SIGNAL_CONCLUSIONS = {"cancelled", "skipped", "neutral", "stale"}
PENDING_STATES = {"pending", "queued", "in_progress", "requested", "waiting"}

EXCEPTION_RE = re.compile(
    r"ImportError|ModuleNotFoundError|AttributeError|TypeError|NameError|"
    r"has no attribute|unexpected keyword|got an unexpected"
)
NON_BUMP_CHECK_RE = re.compile(
    r"ruff|black|flake8|isort|mypy|pyright|pylint|lint|format|style|"
    r"codecov|coverage|docs?|pre-commit",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SearchItem:
    repo: str
    number: int
    title: str
    login: str
    created_at: str


@dataclass(frozen=True)
class _Check:
    name: str
    conclusion: str
    html_url: str | None = None
    job_id: int | None = None


def search_url(bot: str, start: dt.date, end: dt.date, page: int) -> str:
    """Build a search URL for closed PRs by ``bot`` in ``[start, end)``."""
    inclusive_end = end - dt.timedelta(days=1)
    query = (
        "is:pr is:closed "
        f"author:app/{bot} language:python "
        f"closed:{start.isoformat()}..{inclusive_end.isoformat()}"
    )
    params = urllib.parse.urlencode(
        {"q": query, "per_page": PAGE_SIZE, "page": page, "sort": "created", "order": "asc"}
    )
    return f"{github.API_ROOT}/search/issues?{params}"


def _repo_from_payload(raw: dict) -> str | None:
    repository_url = raw.get("repository_url")
    if isinstance(repository_url, str) and "/repos/" in repository_url:
        tail = repository_url.split("/repos/", 1)[1].strip("/")
        parts = tail.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    html_url = raw.get("html_url")
    if isinstance(html_url, str):
        parts = [part for part in urllib.parse.urlparse(html_url).path.split("/") if part]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return None


def _search_item(raw: object) -> SearchItem | None:
    if not isinstance(raw, dict):
        return None
    repo = _repo_from_payload(raw)
    number = raw.get("number")
    title = raw.get("title")
    if repo is None or not isinstance(number, int) or not isinstance(title, str):
        return None
    user = raw.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    created_at = raw.get("created_at")
    return SearchItem(
        repo=repo,
        number=number,
        title=title,
        login=login if isinstance(login, str) else "",
        created_at=created_at if isinstance(created_at, str) else "",
    )


def _fetch_search_page(
    client: GitHubClient, bot: str, start: dt.date, end: dt.date, page: int
) -> tuple[int, list[SearchItem]]:
    payload = client.get_json(search_url(bot, start, end, page))
    if not isinstance(payload, dict):
        raise GitHubError("Unexpected GitHub search response")
    total = payload.get("total_count")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    items = [item for item in map(_search_item, raw_items) if item is not None]
    return (total if isinstance(total, int) else len(items)), items


def _iter_window(
    client: GitHubClient, bot: str, start: dt.date, end: dt.date, depth: int = 0
) -> Iterator[SearchItem]:
    if start >= end:
        return
    total, items = _fetch_search_page(client, bot, start, end, 1)
    days = (end - start).days
    if total >= 1000 and days > 1 and depth < MAX_WINDOW_DEPTH:
        mid = start + dt.timedelta(days=days // 2)
        yield from _iter_window(client, bot, start, mid, depth + 1)
        yield from _iter_window(client, bot, mid, end, depth + 1)
        return
    items.sort(key=lambda item: (item.created_at, item.number))
    yield from items
    page = 1
    while len(items) >= PAGE_SIZE and page < MAX_CHECK_PAGES:
        page += 1
        total, items = _fetch_search_page(client, bot, start, end, page)
        items.sort(key=lambda item: (item.created_at, item.number))
        yield from items


def _next_month(day: dt.date) -> dt.date:
    if day.month == 12:
        return dt.date(day.year + 1, 1, 1)
    return dt.date(day.year, day.month + 1, 1)


def _iter_months(
    client: GitHubClient, bot: str, since: dt.date, until: dt.date
) -> Iterator[SearchItem]:
    start = since
    while start < until:
        end = min(_next_month(start), until)
        yield from _iter_window(client, bot, start, end)
        start = end


def _iter_discoveries(
    client: GitHubClient, bots: list[str], since: dt.date, until: dt.date
) -> Iterator[SearchItem]:
    generators = [_iter_months(client, bot, since, until) for bot in bots]
    active = list(generators)
    while active:
        for generator in list(active):
            try:
                yield next(generator)
            except StopIteration:
                active.remove(generator)


def _excluded(item: SearchItem, reason: ExcludedReason) -> Excluded:
    return Excluded(
        case_id=f"{item.repo}#{item.number}",
        repo=item.repo,
        pr_number=item.number,
        reason=reason,
    )


def _split_for(repo: str) -> str:
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()
    return "test" if int(digest[:2], 16) < TEST_SPLIT_THRESHOLD else "dev"


def _bot_from_login(login: str) -> str | None:
    lowered = login.lower()
    if lowered.startswith("dependabot"):
        return "dependabot"
    if lowered.startswith("renovate"):
        return "renovate"
    return None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _full_name_from(container: object) -> str | None:
    if not isinstance(container, dict):
        return None
    repo = container.get("repo")
    if not isinstance(repo, dict):
        return None
    return _str_or_none(repo.get("full_name"))


def _fetch_files(client: GitHubClient, base_url: str, number: int) -> list[dict]:
    files: list[dict] = []
    page = 1
    while len(files) < MAX_FILES:
        chunk = client.get_json(f"{base_url}/pulls/{number}/files?per_page={PAGE_SIZE}&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
        files.extend(entry for entry in chunk if isinstance(entry, dict))
        if len(chunk) < PAGE_SIZE:
            break
        page += 1
    return files[:MAX_FILES]


def _fetch_side(
    client: GitHubClient, repo_full_name: str, path: str, sha: str, kind: str
) -> bytes | None:
    owner, _, name = repo_full_name.partition("/")
    encoded = urllib.parse.quote(path, safe="/")
    url = f"{github.RAW_ROOT}/{owner}/{name}/{sha}/{encoded}"
    try:
        return client.get_bytes(url)
    except HttpStatusError as exc:
        if exc.status != 404:
            raise
        return b"" if kind == "requirements" else None


def _conclusion_from_state(state: str) -> str:
    return {"success": "success", "failure": "failure", "error": "error"}.get(state, state)


def _fetch_check_runs(client: GitHubClient, base_url: str, sha: str) -> list[dict]:
    runs: list[dict] = []
    page = 1
    while page <= MAX_CHECK_PAGES:
        payload = client.get_json(
            f"{base_url}/commits/{sha}/check-runs?filter=all&per_page={PAGE_SIZE}&page={page}"
        )
        if not isinstance(payload, dict):
            raise GitHubError("Unexpected GitHub check-runs response")
        chunk = payload.get("check_runs")
        if not isinstance(chunk, list):
            chunk = []
        runs.extend(entry for entry in chunk if isinstance(entry, dict))
        if len(chunk) < PAGE_SIZE:
            break
        page += 1
    return runs


def _runs_by_name(runs: list[dict]) -> dict[str, list[dict]]:
    by_name: dict[str, list[dict]] = {}
    for run in runs:
        name = run.get("name")
        if isinstance(name, str) and name:
            by_name.setdefault(name, []).append(run)
    for attempts in by_name.values():
        attempts.sort(
            key=lambda run: (str(run.get("started_at") or ""), _int_or_none(run.get("id")) or 0)
        )
    return by_name


def _collect_base_checks(client: GitHubClient, base_url: str, base_sha: str) -> list[CheckRef]:
    by_name = _runs_by_name(_fetch_check_runs(client, base_url, base_sha))
    checks: list[CheckRef] = []
    for name in sorted(by_name):
        latest = by_name[name][-1]
        status = str(latest.get("status") or "")
        conclusion = latest.get("conclusion")
        if status in PENDING_STATES or not isinstance(conclusion, str):
            continue
        if conclusion != "success" and conclusion not in FAILURE_CONCLUSIONS:
            continue
        checks.append(
            CheckRef(
                name=name,
                conclusion=conclusion,
                html_url=_str_or_none(latest.get("html_url")),
            )
        )
    return checks


def _collect_checks(
    client: GitHubClient, base_url: str, head_sha: str
) -> tuple[list[_Check], bool, bool]:
    runs = _fetch_check_runs(client, base_url, head_sha)
    by_name = _runs_by_name(runs)

    checks: list[_Check] = []
    pending = False
    flaky = False
    for name in sorted(by_name):
        attempts = by_name[name]
        latest = attempts[-1]
        status = str(latest.get("status") or "")
        conclusion = latest.get("conclusion")
        if status in PENDING_STATES or conclusion is None:
            pending = True
            continue
        conclusion = str(conclusion)
        earlier_failed = any(
            str(attempt.get("conclusion") or "") in FAILURE_CONCLUSIONS for attempt in attempts[:-1]
        )
        if earlier_failed and conclusion == "success":
            flaky = True
        if conclusion in NON_SIGNAL_CONCLUSIONS:
            continue
        if conclusion != "success" and conclusion not in FAILURE_CONCLUSIONS:
            continue
        checks.append(
            _Check(
                name=name,
                conclusion=conclusion,
                html_url=_str_or_none(latest.get("html_url")),
                job_id=_int_or_none(latest.get("id")),
            )
        )

    status_payload = client.get_json(f"{base_url}/commits/{head_sha}/status")
    statuses = status_payload.get("statuses") if isinstance(status_payload, dict) else None
    if not isinstance(statuses, list):
        statuses = []
    known_names = set(by_name)
    for raw in statuses:
        if not isinstance(raw, dict):
            continue
        context = raw.get("context")
        if not isinstance(context, str) or not context or context in known_names:
            continue
        state = str(raw.get("state") or "")
        if state == "pending":
            pending = True
            continue
        conclusion = _conclusion_from_state(state)
        if conclusion in NON_SIGNAL_CONCLUSIONS:
            continue
        if conclusion != "success" and conclusion not in FAILURE_CONCLUSIONS:
            continue
        checks.append(
            _Check(
                name=context,
                conclusion=conclusion,
                html_url=_str_or_none(raw.get("target_url")),
            )
        )

    checks.sort(key=lambda check: (check.name, check.html_url or ""))
    return checks, flaky, pending


def _import_names(package: str) -> list[str]:
    underscored = package.replace("-", "_")
    candidates: set[str] = set()
    for value in (package, underscored):
        candidates.add(value)
        parts = re.split(r"[-_]", value)
        candidates.add(parts[0])
        candidates.add(parts[-1])
    return sorted(name.lower() for name in candidates if len(name) >= 3)


def _collect_excerpts(
    client: GitHubClient, base_url: str, checks: list[_Check], package: str
) -> list[FailureExcerpt]:
    names = _import_names(package)
    excerpts: list[FailureExcerpt] = []
    total_lines = 0
    total_bytes = 0
    for check in checks:
        if total_lines >= MAX_LOG_LINES_PER_CASE or total_bytes >= MAX_LOG_BYTES_PER_CASE:
            break
        if check.job_id is None:
            continue
        url = f"{base_url}/actions/jobs/{check.job_id}/logs"
        try:
            text = client.get_log(url)
        except HttpStatusError:
            continue
        matched: list[str] = []
        for line in text.splitlines():
            if not EXCEPTION_RE.search(line):
                continue
            lowered = line.lower()
            if not any(name in lowered for name in names):
                continue
            matched.append(line.strip()[:MAX_LOG_LINE_LENGTH])
            if len(matched) >= MAX_LOG_LINES_PER_CHECK:
                break
        if not matched:
            continue
        size = sum(len(line.encode("utf-8")) + 1 for line in matched)
        if total_bytes + size > MAX_LOG_BYTES_PER_CASE:
            break
        excerpts.append(FailureExcerpt(check_name=check.name, lines=matched))
        total_lines += len(matched)
        total_bytes += size
    return excerpts


def _commit_from_payload(raw: object) -> LaterFix | None:
    if not isinstance(raw, dict):
        return None
    sha = raw.get("sha")
    commit = raw.get("commit")
    if not isinstance(sha, str) or not isinstance(commit, dict):
        return None
    message = commit.get("message")
    date = None
    for key in ("committer", "author"):
        person = commit.get(key)
        if isinstance(person, dict) and isinstance(person.get("date"), str):
            date = person["date"]
            break
    if not isinstance(message, str) or not isinstance(date, str):
        return None
    first_line = message.splitlines()[0] if message.splitlines() else ""
    return LaterFix(sha=sha, message=first_line[:200], date=date)


def _later_fixes(
    client: GitHubClient,
    base_url: str,
    *,
    ref: str,
    since: str,
    lockfile_path: str,
    package: str,
) -> list[LaterFix]:
    found: dict[str, LaterFix] = {}
    import_names = _import_names(package)
    calls = 0
    while calls < LATER_FIX_CALL_BUDGET and len(found) < MAX_LATER_FIXES:
        params = {"sha": ref, "since": since, "per_page": "100"}
        if calls == 0:
            params["path"] = lockfile_path
        payload = client.get_json(f"{base_url}/commits?{urllib.parse.urlencode(params)}")
        calls += 1
        if not isinstance(payload, list):
            break
        for raw in payload:
            fix = _commit_from_payload(raw)
            if fix is None:
                continue
            if calls > 1:
                lowered = fix.message.lower()
                if not any(name in lowered for name in import_names) and package not in lowered:
                    continue
            found.setdefault(fix.sha, fix)
    ordered = sorted(found.values(), key=lambda fix: (fix.date, fix.sha))
    return ordered[:MAX_LATER_FIXES]


def _mine_case(client: GitHubClient, item: SearchItem, *, logs: bool) -> Candidate | Excluded:
    owner, _, name = item.repo.partition("/")
    base_url = f"{github.API_ROOT}/repos/{owner}/{name}"
    bot = _bot_from_login(item.login)
    if bot is None:
        return _excluded(item, "other")

    title_result = classify_title(item.title, cast(Bot, bot))
    if title_result.parsed is None:
        return _excluded(item, title_result.reason or "other")

    pr = client.get_json(f"{base_url}/pulls/{item.number}")
    if not isinstance(pr, dict):
        return _excluded(item, "other")
    title = pr.get("title") if isinstance(pr.get("title"), str) else item.title
    if title != item.title:
        title_result = classify_title(title, cast(Bot, bot))
        if title_result.parsed is None:
            return _excluded(item, title_result.reason or "other")
    parsed = title_result.parsed

    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base_sha = base.get("sha")
    head_sha = head.get("sha")
    if not isinstance(base_sha, str) or not isinstance(head_sha, str):
        return _excluded(item, "other")
    base_repo = _full_name_from(base) or item.repo
    head_repo = _full_name_from(head) or base_repo
    default_ref = _str_or_none(base.get("ref"))
    merged = bool(pr.get("merged"))
    merged_at = _str_or_none(pr.get("merged_at"))
    closed_at = _str_or_none(pr.get("closed_at"))

    selected = [
        entry
        for entry in _fetch_files(client, base_url, item.number)
        if lockfile_kind(str(entry.get("filename", ""))) is not None
    ]
    if not selected:
        return _excluded(item, "no_supported_lockfile")

    bumps: set[tuple[str, str | None, str | None]] = set()
    paths: list[str] = []
    parsed_any = False
    for entry in selected:
        new_path = str(entry.get("filename") or "")
        kind = lockfile_kind(new_path)
        if kind is None:
            continue
        if entry.get("status") == "renamed":
            old_path = str(entry.get("previous_filename") or new_path)
        else:
            old_path = new_path
        old_bytes = _fetch_side(client, base_repo, old_path, base_sha, kind)
        if old_bytes is None:
            continue
        new_bytes = _fetch_side(client, head_repo, new_path, head_sha, kind)
        if new_bytes is None:
            continue
        try:
            result = parse_lock_pair(
                old_bytes.decode("utf-8-sig"), new_bytes.decode("utf-8-sig"), kind
            )
        except (UnicodeDecodeError, LockParseError):
            return _excluded(item, "other")
        parsed_any = True
        paths.append(new_path)
        for bump in result.bumps:
            bumps.add((bump.package, bump.old_version, bump.new_version))
    if not parsed_any:
        return _excluded(item, "other")

    if len(bumps) != 1:
        return _excluded(item, "parse_mismatch")
    package, old_version, new_version = next(iter(bumps))
    if package != canonicalize_name(parsed.name) or new_version is None:
        return _excluded(item, "parse_mismatch")
    if bot == "dependabot":
        if old_version != parsed.old_version or new_version != parsed.new_version:
            return _excluded(item, "parse_mismatch")
    else:
        if not loose_version_match(parsed.new_version, new_version):
            return _excluded(item, "parse_mismatch")
        if parsed.old_version is not None and old_version != parsed.old_version:
            return _excluded(item, "parse_mismatch")

    checks, flaky, pending = _collect_checks(client, base_url, head_sha)
    if pending:
        return _excluded(item, "other")
    if not checks:
        return _excluded(item, "closed_without_ci_signal" if not merged else "no_ci")
    failed = [check for check in checks if check.conclusion in FAILURE_CONCLUSIONS]
    if failed and all(NON_BUMP_CHECK_RE.search(check.name) for check in failed):
        return _excluded(item, "ci_not_attributable")

    ci_summary = "some_failed" if failed else "all_passed"
    base_checks: list[CheckRef] = []
    comparisons: list[FailureVsBase] = []
    if failed:
        try:
            base_checks = _collect_base_checks(client, base_url, base_sha)
        except Exception:
            base_checks = []
        comparisons = _compare_failures(failed, base_checks)
        statuses = {comparison.status for comparison in comparisons}
        if "regression" in statuses or "no_base_run" in statuses:
            label_suggestion = "BROKE_SUSPECT"
        else:
            label_suggestion = "UNCLEAR"
    elif merged:
        label_suggestion = "OK_SUSPECT"
    else:
        label_suggestion = "UNCLEAR"

    failure_excerpts: list[FailureExcerpt] = []
    if logs and failed:
        failure_excerpts = _collect_excerpts(client, base_url, failed, package)

    lockfile_paths = sorted(set(paths))
    later_fixes: list[LaterFix] = []
    event_date = merged_at or closed_at
    if label_suggestion == "BROKE_SUSPECT" and default_ref and event_date:
        later_fixes = _later_fixes(
            client,
            base_url,
            ref=default_ref,
            since=event_date,
            lockfile_path=lockfile_paths[0],
            package=package,
        )

    return Candidate(
        case_id=f"{item.repo}#{item.number}",
        repo=item.repo,
        pr_number=item.number,
        bot=bot,
        title=title,
        package=package,
        old_version=old_version,
        new_version=new_version,
        base_sha=base_sha,
        head_sha=head_sha,
        merged=merged,
        merged_at=merged_at,
        lockfile_kind=lockfile_kind(lockfile_paths[0]),
        lockfile_paths=lockfile_paths,
        ci_checks=[
            CheckRef(name=check.name, conclusion=check.conclusion, html_url=check.html_url)
            for check in checks
        ],
        base_ci_checks=base_checks,
        ci_summary=ci_summary,
        failure_vs_base=comparisons,
        label_suggestion=label_suggestion,
        failure_excerpts=failure_excerpts,
        later_fix_commits=later_fixes,
        split=_split_for(item.repo),
        rerun_flaky=flaky,
    )


def _compare_failures(failed: list[_Check], base_checks: list[CheckRef]) -> list[FailureVsBase]:
    base_conclusions = {check.name: check.conclusion for check in base_checks}
    comparisons: list[FailureVsBase] = []
    for check in failed:
        base = base_conclusions.get(check.name)
        if base is None:
            status: BaseComparisonStatus = "no_base_run"
        elif base in FAILURE_CONCLUSIONS:
            status = "preexisting"
        elif base == "success":
            status = "regression"
        else:
            status = "no_base_run"
        comparisons.append(FailureVsBase(name=check.name, status=status))
    return comparisons


def _mine_safely(client: GitHubClient, item: SearchItem, *, logs: bool) -> Candidate | Excluded:
    try:
        return _mine_case(client, item, logs=logs)
    except HttpStatusError:
        return _excluded(item, "other")


def _load_records(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    records: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("case_id"), str):
            records[record["case_id"]] = record
    return records


_replace = os.replace
_replace_sleep = time.sleep


def _replace_with_retry(tmp: Path, path: Path) -> None:
    for attempt in range(REPLACE_RETRIES + 1):
        try:
            _replace(tmp, path)
            return
        except PermissionError:
            if attempt >= REPLACE_RETRIES:
                raise
            _replace_sleep(REPLACE_RETRY_SLEEP)


def _write_jsonl(path: Path, records: dict[str, dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            for case_id in sorted(records):
                handle.write(json.dumps(records[case_id], ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _build_summary(
    candidates: dict[str, dict], excluded: dict[str, dict], client: GitHubClient
) -> Summary:
    labels = Counter(str(record.get("label_suggestion", "")) for record in candidates.values())
    reasons = Counter(str(record.get("reason", "")) for record in excluded.values())
    bots = Counter(str(record.get("bot", "")) for record in candidates.values())
    splits = Counter(str(record.get("split", "")) for record in candidates.values())
    repos = {str(record.get("repo", "")) for record in candidates.values()}
    return Summary(
        kept=len(candidates),
        excluded=len(excluded),
        distinct_repos=len(repos),
        labels={key: labels[key] for key in sorted(labels)},
        reasons={key: reasons[key] for key in sorted(reasons)},
        bots={key: bots[key] for key in sorted(bots)},
        splits={key: splits[key] for key in sorted(splits)},
        api_calls=client.api_calls,
        cache_hits=client.cache_hits,
    )


def _write_summary(
    out: Path, candidates: dict[str, dict], excluded: dict[str, dict], client: GitHubClient
) -> Summary:
    summary = _build_summary(candidates, excluded, client)
    (out / "summary.json").write_text(
        json.dumps(summary.model_dump(), indent=2) + "\n", encoding="utf-8"
    )
    return summary


def mine(
    *,
    out: Path,
    bot: str = "both",
    since: dt.date | None = None,
    until: dt.date | None = None,
    limit: int = 60,
    logs: bool = False,
    offline: bool = False,
    cache_dir: Path | None = None,
    client: GitHubClient | None = None,
) -> int:
    """Mine candidates into ``out``; returns a process exit code."""
    if bot not in {"dependabot", "renovate", "both"}:
        raise ValueError(f"Unknown bot choice: {bot!r}")
    if limit < 0:
        raise ValueError("--limit must be zero or greater")

    until_date = until or dt.date.today()
    since_date = since or (until_date - dt.timedelta(days=DEFAULT_SINCE_DAYS))
    if since_date >= until_date:
        raise ValueError("--since must be earlier than --until")

    active_client = client or GitHubClient(cache_dir=cache_dir, offline=offline)
    if logs and not active_client.token:
        raise GitHubError("--logs requires GITHUB_TOKEN")

    out.mkdir(parents=True, exist_ok=True)
    candidates_path = out / "candidates.jsonl"
    excluded_path = out / "excluded.jsonl"
    candidates = _load_records(candidates_path)
    excluded = _load_records(excluded_path)
    seen = set(candidates) | set(excluded)

    try:
        if len(candidates) < limit:
            bots = ["dependabot", "renovate"] if bot == "both" else [bot]
            for item in _iter_discoveries(active_client, bots, since_date, until_date):
                case_id = f"{item.repo}#{item.number}"
                if case_id in seen:
                    continue
                result = _mine_safely(active_client, item, logs=logs)
                if isinstance(result, Candidate):
                    candidates[case_id] = result.model_dump()
                    outcome = f"kept ({result.label_suggestion})"
                else:
                    excluded[case_id] = result.model_dump()
                    outcome = f"excluded ({result.reason})"
                seen.add(case_id)
                _write_jsonl(candidates_path, candidates)
                _write_jsonl(excluded_path, excluded)
                print(
                    f"[{len(candidates)}/{limit}] {case_id} -> {outcome}",
                    file=sys.stderr,
                )
                if len(candidates) >= limit:
                    break
    except KeyboardInterrupt:
        _write_jsonl(candidates_path, candidates)
        _write_jsonl(excluded_path, excluded)
        _write_summary(out, candidates, excluded, active_client)
        print(
            f"interrupted; saved {len(candidates)} candidate(s) and "
            f"{len(excluded)} exclusion(s) to {out}",
            file=sys.stderr,
        )
        return 130

    _write_jsonl(candidates_path, candidates)
    _write_jsonl(excluded_path, excluded)
    summary = _write_summary(out, candidates, excluded, active_client)

    print(
        f"Mined {summary.kept} candidate(s) and {summary.excluded} exclusion(s) "
        f"across {summary.distinct_repos} repo(s)."
    )
    if summary.labels:
        print("  labels: " + ", ".join(f"{key}={value}" for key, value in summary.labels.items()))
    if summary.reasons:
        print("  reasons: " + ", ".join(f"{key}={value}" for key, value in summary.reasons.items()))
    print(f"  api calls: {summary.api_calls} (cache hits: {summary.cache_hits})")
    print(f"  wrote: {candidates_path}, {excluded_path}, {out / 'summary.json'}")
    return 0
