from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from datetime import date
from pathlib import Path

import pytest

import bumpcheck.bench.mine as bench_mine
from bumpcheck.bench.github import GitHubClient, GitHubError
from bumpcheck.bench.mine import _iter_window, mine, search_url

OWNER = "acme"
REPO = "widgets"
REPO_FULL = f"{OWNER}/{REPO}"
API = "https://api.github.com"
BLOB_URL = "https://blob.example/log"
BASE_SHA = "base000"
HEAD_SHA = "head111"
PR_NUMBER = 1

OLD_UV = 'version = 1\n\n[[package]]\nname = "requests"\nversion = "2.31.0"\n'
NEW_UV = 'version = 1\n\n[[package]]\nname = "requests"\nversion = "2.32.0"\n'
FLASK_UV = 'version = 1\n\n[[package]]\nname = "flask"\nversion = "3.0.0"\n'

LOG_TEXT = (
    "2026-01-02T00:00:00Z ImportError: cannot import name 'legacy' from 'requests'\n"
    "2026-01-02T00:00:01Z AttributeError: 'NoneType' object has no attribute 'x' in flask\n"
    "2026-01-02T00:00:02Z TypeError: requests.get() got an unexpected keyword argument 'verify'\n"
    "2026-01-02T00:00:03Z tests passed\n"
)

SUCCESS_CHECK = {
    "id": 9001,
    "name": "tests",
    "status": "completed",
    "conclusion": "success",
    "started_at": "2026-01-02T00:00:00Z",
    "html_url": "https://ci/tests",
}
FAILURE_CHECK = {
    "id": 9001,
    "name": "tests",
    "status": "completed",
    "conclusion": "failure",
    "started_at": "2026-01-02T00:00:00Z",
    "html_url": "https://ci/tests",
}


def _json(payload: object) -> tuple[int, dict, bytes]:
    return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")


class FakeGitHub:
    def __init__(self, **config: object) -> None:
        self.title = config.pop("title", "Bump requests from 2.31.0 to 2.32.0")
        self.login = config.pop("login", "dependabot[bot]")
        self.merged = config.pop("merged", True)
        self.merged_at = config.pop("merged_at", "2026-01-05T10:00:00Z")
        self.closed_at = config.pop("closed_at", "2026-01-05T10:00:00Z")
        self.check_runs = config.pop("check_runs", None)
        self.statuses = config.pop("statuses", None)
        self.commits = config.pop("commits", None)
        self.log_text = config.pop("log_text", None)
        self.files = config.pop("files", None)
        self.old_text = config.pop("old_text", OLD_UV)
        self.new_text = config.pop("new_text", NEW_UV)
        self.calls: list[dict] = []
        assert not config, f"unexpected config: {config}"

    @property
    def search_item(self) -> dict:
        return {
            "number": PR_NUMBER,
            "title": self.title,
            "user": {"login": self.login},
            "created_at": "2026-01-01T00:00:00Z",
            "repository_url": f"{API}/repos/{REPO_FULL}",
            "html_url": f"https://github.com/{REPO_FULL}/pull/{PR_NUMBER}",
        }

    @property
    def pr_payload(self) -> dict:
        return {
            "title": self.title,
            "merged": self.merged,
            "merged_at": self.merged_at if self.merged else None,
            "closed_at": self.closed_at,
            "base": {"sha": BASE_SHA, "ref": "main", "repo": {"full_name": REPO_FULL}},
            "head": {"sha": HEAD_SHA, "repo": {"full_name": REPO_FULL}},
        }

    def _check_runs(self) -> list[dict]:
        if self.check_runs is not None:
            return self.check_runs  # type: ignore[return-value]
        return [SUCCESS_CHECK]

    def _statuses(self) -> list[dict]:
        return self.statuses if self.statuses is not None else []  # type: ignore[return-value]

    def _commits(self) -> list[dict]:
        if self.commits is not None:
            return self.commits  # type: ignore[return-value]
        return [
            {
                "sha": "fix123",
                "commit": {
                    "message": "Fix requests import\n\nlong body",
                    "committer": {"date": "2026-01-06T00:00:00Z"},
                },
            }
        ]

    def _files(self) -> list[dict]:
        if self.files is not None:
            return self.files  # type: ignore[return-value]
        return [{"filename": "uv.lock", "status": "modified"}]

    def __call__(self, method: str, url: str, headers: dict) -> tuple:
        self.calls.append({"url": url, "headers": dict(headers)})
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.netloc == "raw.githubusercontent.com":
            parts = path.lstrip("/").split("/")
            body = self.old_text if parts[2] == BASE_SHA else self.new_text
            return 200, {}, str(body).encode("utf-8")
        if path == "/search/issues":
            return _json({"total_count": 1, "items": [self.search_item]})
        base = f"/repos/{REPO_FULL}"
        if path == f"{base}/pulls/{PR_NUMBER}":
            return _json(self.pr_payload)
        if path == f"{base}/pulls/{PR_NUMBER}/files":
            return _json(self._files())
        if path == f"{base}/commits/{HEAD_SHA}/check-runs":
            return _json({"total_count": len(self._check_runs()), "check_runs": self._check_runs()})
        if path == f"{base}/commits/{HEAD_SHA}/status":
            return _json({"statuses": self._statuses()})
        if path == f"{base}/commits":
            return _json(self._commits())
        if path == f"{base}/actions/jobs/9001/logs":
            return 302, {"Location": BLOB_URL}, b""
        if url == BLOB_URL:
            return 200, {"Content-Type": "text/plain"}, str(self.log_text or LOG_TEXT).encode()
        raise AssertionError(f"unexpected URL: {url}")


class FakeFullPage(FakeGitHub):
    """Serves one full search page so pagination and early stopping are observable."""

    def __call__(self, method: str, url: str, headers: dict) -> tuple:
        if urllib.parse.urlparse(url).path == "/search/issues":
            self.calls.append({"url": url, "headers": dict(headers)})
            items = []
            for number in range(1, 101):
                item = dict(self.search_item)
                item["number"] = number
                item["html_url"] = f"https://github.com/{REPO_FULL}/pull/{number}"
                items.append(item)
            return _json({"total_count": 100, "items": items})
        return super().__call__(method, url, headers)


class FakeMulti(FakeGitHub):
    """Serves several distinct PRs from one search page, optionally interrupting."""

    def __init__(self, titles: list[str], *, interrupt_at: int | None = None) -> None:
        super().__init__(title=titles[0])
        self.titles = titles
        self.interrupt_at = interrupt_at

    def __call__(self, method: str, url: str, headers: dict) -> tuple:
        path = urllib.parse.urlparse(url).path
        if path == "/search/issues":
            self.calls.append({"url": url, "headers": dict(headers)})
            items = []
            for number, title in enumerate(self.titles, start=1):
                item = dict(self.search_item)
                item["number"] = number
                item["title"] = title
                item["html_url"] = f"https://github.com/{REPO_FULL}/pull/{number}"
                items.append(item)
            return _json({"total_count": len(items), "items": items})
        prefix = f"/repos/{REPO_FULL}/pulls/"
        if path.startswith(prefix):
            number_text, _, suffix = path[len(prefix) :].partition("/")
            if number_text.isdigit():
                number = int(number_text)
                if not suffix:
                    if self.interrupt_at is not None and number >= self.interrupt_at:
                        raise KeyboardInterrupt
                    self.calls.append({"url": url, "headers": dict(headers)})
                    payload = dict(self.pr_payload)
                    payload["title"] = self.titles[number - 1]
                    return _json(payload)
                return super().__call__(method, f"{API}{prefix}{PR_NUMBER}/{suffix}", headers)
        return super().__call__(method, url, headers)


def _mine_into(
    tmp_path: Path,
    fake: FakeGitHub,
    *,
    out_name: str = "out",
    cache_name: str = "cache",
    logs: bool = False,
    limit: int = 10,
) -> tuple[Path, int]:
    client = GitHubClient(
        token="test-token",
        transport=fake,
        sleep=lambda seconds: None,
        now=lambda: 1000.0,
        rng=lambda: 0.0,
        cache_dir=tmp_path / cache_name,
    )
    out = tmp_path / out_name
    code = mine(
        out=out,
        since=date(2026, 1, 1),
        until=date(2026, 2, 1),
        limit=limit,
        logs=logs,
        client=client,
    )
    return out, code


def _run(
    tmp_path: Path,
    fake: FakeGitHub,
    *,
    out_name: str = "out",
    cache_name: str = "cache",
    logs: bool = False,
    limit: int = 10,
) -> Path:
    out, _ = _mine_into(
        tmp_path, fake, out_name=out_name, cache_name=cache_name, logs=logs, limit=limit
    )
    return out


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _expected_split(repo: str) -> str:
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()
    return "test" if int(digest[:2], 16) < 77 else "dev"


def _candidate(tmp_path: Path, fake: FakeGitHub, **kwargs: object) -> tuple[dict, Path]:
    out = _run(tmp_path, fake, **kwargs)
    candidates = _read_jsonl(out / "candidates.jsonl")
    assert len(candidates) == 1
    return candidates[0], out


def _excluded(tmp_path: Path, fake: FakeGitHub, **kwargs: object) -> dict:
    out = _run(tmp_path, fake, **kwargs)
    assert _read_jsonl(out / "candidates.jsonl") == []
    excluded = _read_jsonl(out / "excluded.jsonl")
    assert len(excluded) == 1
    return excluded[0]


def test_search_url_uses_closed_windows() -> None:
    url = search_url("dependabot", date(2026, 1, 1), date(2026, 2, 1), 1)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
    assert query == (
        "is:pr is:closed author:app/dependabot language:python closed:2026-01-01..2026-01-31"
    )


def test_ok_suspect_candidate(tmp_path: Path) -> None:
    candidate, out = _candidate(tmp_path, FakeGitHub())

    assert candidate["case_id"] == f"{REPO_FULL}#{PR_NUMBER}"
    assert candidate["repo"] == REPO_FULL
    assert candidate["pr_number"] == PR_NUMBER
    assert candidate["bot"] == "dependabot"
    assert candidate["package"] == "requests"
    assert candidate["old_version"] == "2.31.0"
    assert candidate["new_version"] == "2.32.0"
    assert candidate["base_sha"] == BASE_SHA
    assert candidate["head_sha"] == HEAD_SHA
    assert candidate["merged"] is True
    assert candidate["merged_at"] == "2026-01-05T10:00:00Z"
    assert candidate["lockfile_kind"] == "uv.lock"
    assert candidate["lockfile_paths"] == ["uv.lock"]
    assert candidate["ci_checks"] == [
        {"name": "tests", "conclusion": "success", "html_url": "https://ci/tests"}
    ]
    assert candidate["ci_summary"] == "all_passed"
    assert candidate["label_suggestion"] == "OK_SUSPECT"
    assert candidate["failure_excerpts"] == []
    assert candidate["later_fix_commits"] == []
    assert candidate["split"] == _expected_split(REPO_FULL)
    assert candidate["rerun_flaky"] is False
    assert candidate["label"] is None
    assert "mined_at" not in candidate
    assert (out / "excluded.jsonl").read_text(encoding="utf-8") == ""

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["kept"] == 1
    assert summary["excluded"] == 0
    assert summary["distinct_repos"] == 1
    assert summary["labels"] == {"OK_SUSPECT": 1}
    assert summary["bots"] == {"dependabot": 1}
    assert summary["splits"] == {candidate["split"]: 1}
    assert summary["reasons"] == {}


def test_broke_suspect_records_failure_and_later_fix(tmp_path: Path) -> None:
    candidate, _ = _candidate(tmp_path, FakeGitHub(check_runs=[FAILURE_CHECK]))

    assert candidate["ci_summary"] == "some_failed"
    assert candidate["label_suggestion"] == "BROKE_SUSPECT"
    assert candidate["ci_checks"] == [
        {"name": "tests", "conclusion": "failure", "html_url": "https://ci/tests"}
    ]
    assert candidate["later_fix_commits"] == [
        {
            "sha": "fix123",
            "message": "Fix requests import",
            "date": "2026-01-06T00:00:00Z",
        }
    ]


def test_later_fix_uses_at_most_two_calls(tmp_path: Path) -> None:
    fake = FakeGitHub(check_runs=[FAILURE_CHECK])
    _candidate(tmp_path, fake)
    commit_calls = [
        call for call in fake.calls if urllib.parse.urlparse(call["url"]).path.endswith("/commits")
    ]
    assert len(commit_calls) == 2


def test_closed_unmerged_all_passed_is_unclear(tmp_path: Path) -> None:
    candidate, _ = _candidate(tmp_path, FakeGitHub(merged=False))
    assert candidate["label_suggestion"] == "UNCLEAR"
    assert candidate["later_fix_commits"] == []


def test_lint_only_failure_is_not_attributable(tmp_path: Path) -> None:
    lint_check = dict(FAILURE_CHECK, name="ruff (/ lint)")
    excluded = _excluded(tmp_path, FakeGitHub(check_runs=[lint_check]))
    assert excluded["reason"] == "ci_not_attributable"


def test_no_ci_reasons(tmp_path: Path) -> None:
    merged = _excluded(tmp_path, FakeGitHub(check_runs=[], statuses=[]), out_name="out1")
    assert merged["reason"] == "no_ci"

    closed = _excluded(
        tmp_path,
        FakeGitHub(check_runs=[], statuses=[], merged=False),
        out_name="out2",
    )
    assert closed["reason"] == "closed_without_ci_signal"


def test_pending_check_is_other(tmp_path: Path) -> None:
    pending = {
        "id": 9001,
        "name": "tests",
        "status": "in_progress",
        "conclusion": None,
        "started_at": "2026-01-02T00:00:00Z",
    }
    excluded = _excluded(tmp_path, FakeGitHub(check_runs=[pending]))
    assert excluded["reason"] == "other"


def test_non_signal_conclusions_are_ignored(tmp_path: Path) -> None:
    checks = [
        dict(SUCCESS_CHECK),
        dict(SUCCESS_CHECK, id=9002, name="optional", conclusion="skipped"),
        dict(SUCCESS_CHECK, id=9003, name="doomed", conclusion="cancelled"),
        dict(SUCCESS_CHECK, id=9004, name="stale-check", conclusion="stale"),
    ]
    candidate, _ = _candidate(tmp_path, FakeGitHub(check_runs=checks))
    assert candidate["ci_summary"] == "all_passed"
    assert [check["name"] for check in candidate["ci_checks"]] == ["tests"]


def test_rerun_flaky_uses_latest_attempt(tmp_path: Path) -> None:
    earlier = dict(FAILURE_CHECK, id=1, started_at="2026-01-02T00:00:00Z")
    later = dict(SUCCESS_CHECK, id=2, started_at="2026-01-03T00:00:00Z")
    candidate, _ = _candidate(tmp_path, FakeGitHub(check_runs=[earlier, later]))
    assert candidate["rerun_flaky"] is True
    assert candidate["ci_summary"] == "all_passed"
    assert candidate["ci_checks"] == [
        {"name": "tests", "conclusion": "success", "html_url": "https://ci/tests"}
    ]


def test_logs_keep_only_package_exception_lines(tmp_path: Path) -> None:
    fake = FakeGitHub(check_runs=[FAILURE_CHECK])
    candidate, _ = _candidate(tmp_path, fake, logs=True)
    assert candidate["failure_excerpts"] == [
        {
            "check_name": "tests",
            "lines": [
                "2026-01-02T00:00:00Z ImportError: cannot import name 'legacy' from 'requests'",
                (
                    "2026-01-02T00:00:02Z TypeError: requests.get() got an unexpected "
                    "keyword argument 'verify'"
                ),
            ],
        }
    ]
    blob_calls = [call for call in fake.calls if call["url"] == BLOB_URL]
    assert len(blob_calls) == 1
    assert "Authorization" not in blob_calls[0]["headers"]


def test_logs_are_capped_per_check(tmp_path: Path) -> None:
    log_text = "\n".join(
        f"line {index}: ImportError: requests.legacy is gone" for index in range(10)
    )
    fake = FakeGitHub(check_runs=[FAILURE_CHECK], log_text=log_text)
    candidate, _ = _candidate(tmp_path, fake, logs=True)
    assert len(candidate["failure_excerpts"][0]["lines"]) == 4


def test_logs_require_a_token(tmp_path: Path) -> None:
    client = GitHubClient(
        token="",
        transport=FakeGitHub(check_runs=[FAILURE_CHECK]),
        sleep=lambda seconds: None,
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(GitHubError):
        mine(
            out=tmp_path / "out",
            since=date(2026, 1, 1),
            until=date(2026, 2, 1),
            logs=True,
            client=client,
        )


def test_group_title_is_excluded_before_fetching_the_pr(tmp_path: Path) -> None:
    fake = FakeGitHub(title="Bump the pip group across 1 directory with 3 updates")
    excluded = _excluded(tmp_path, fake)
    assert excluded["reason"] == "group_bump"
    assert not any("/pulls/" in call["url"] for call in fake.calls)


def test_parse_mismatch_when_title_disagrees_with_lockfile(tmp_path: Path) -> None:
    excluded = _excluded(tmp_path, FakeGitHub(new_text=FLASK_UV))
    assert excluded["reason"] == "parse_mismatch"


def test_no_supported_lockfile(tmp_path: Path) -> None:
    excluded = _excluded(tmp_path, FakeGitHub(files=[{"filename": "README.md"}]))
    assert excluded["reason"] == "no_supported_lockfile"


def test_renovate_loose_version_match(tmp_path: Path) -> None:
    candidate, _ = _candidate(
        tmp_path,
        FakeGitHub(title="Update dependency requests to v2", login="renovate[bot]"),
    )
    assert candidate["bot"] == "renovate"
    assert candidate["old_version"] == "2.31.0"
    assert candidate["new_version"] == "2.32.0"
    assert candidate["label_suggestion"] == "OK_SUSPECT"


def test_dependabot_version_match_is_exact(tmp_path: Path) -> None:
    excluded = _excluded(tmp_path, FakeGitHub(title="Bump requests from 2.31.0 to 2"))
    assert excluded["reason"] == "parse_mismatch"


def test_identical_reruns_produce_identical_files(tmp_path: Path) -> None:
    first = _run(tmp_path, FakeGitHub(), out_name="out1", cache_name="cache1")
    second = _run(tmp_path, FakeGitHub(), out_name="out2", cache_name="cache2")
    for name in ("candidates.jsonl", "excluded.jsonl", "summary.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_limit_counts_existing_candidates(tmp_path: Path) -> None:
    fake = FakeGitHub()
    out = _run(tmp_path, fake, limit=1)
    calls_after_first = len(fake.calls)
    assert len(_read_jsonl(out / "candidates.jsonl")) == 1

    _run(tmp_path, fake, limit=1)

    assert len(fake.calls) == calls_after_first
    assert len(_read_jsonl(out / "candidates.jsonl")) == 1


def test_lazy_discovery_stops_paginating_once_limit_is_reached(tmp_path: Path) -> None:
    fake = FakeFullPage()
    out = _run(tmp_path, fake, limit=1)

    search_calls = [call for call in fake.calls if "/search/issues" in call["url"]]
    assert len(search_calls) == 1
    assert "page=2" not in search_calls[0]["url"]
    assert len(_read_jsonl(out / "candidates.jsonl")) == 1


def test_resume_skips_seen_cases(tmp_path: Path) -> None:
    fake = FakeGitHub()
    out = _run(tmp_path, fake, limit=1)
    before = (out / "candidates.jsonl").read_bytes()

    _run(tmp_path, fake, limit=2)

    assert (out / "candidates.jsonl").read_bytes() == before
    assert _read_jsonl(out / "excluded.jsonl") == []
    pr_calls = [call for call in fake.calls if "/pulls/" in call["url"]]
    assert len(pr_calls) == 2


class _StubClient:
    def __init__(self, total: int, items: list[dict]) -> None:
        self.total = total
        self.items = items
        self.urls: list[str] = []

    def get_json(self, url: str) -> dict:
        self.urls.append(url)
        return {"total_count": self.total, "items": self.items}


def test_windows_split_when_search_is_capped() -> None:
    stub = _StubClient(total=1000, items=[])
    items = list(_iter_window(stub, "dependabot", date(2026, 1, 1), date(2026, 1, 15)))
    assert items == []
    assert len(stub.urls) > 1
    for url in stub.urls:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
        window = query.split("closed:")[1].split()[0]
        start_text, end_text = window.split("..")
        assert date.fromisoformat(start_text) >= date(2026, 1, 1)
        assert date.fromisoformat(end_text) <= date(2026, 1, 14)


def test_progress_lines_report_kept_and_excluded_without_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    titles = [
        "Bump the pip group across 1 directory with 3 updates",
        "Bump requests from 2.31.0 to 2.32.0",
    ]
    fake = FakeMulti(titles)
    out, code = _mine_into(tmp_path, fake, limit=10)
    err = capsys.readouterr().err

    assert code == 0
    assert f"[0/10] {REPO_FULL}#1 -> excluded (group_bump)" in err
    assert f"[1/10] {REPO_FULL}#2 -> kept (OK_SUSPECT)" in err
    progress = [line for line in err.splitlines() if " -> " in line]
    assert len(progress) == 2
    for line in progress:
        assert "http" not in line
        assert "?" not in line
    assert "test-token" not in err
    assert {path.name for path in out.iterdir()} == {
        "candidates.jsonl",
        "excluded.jsonl",
        "summary.json",
    }


def _sorted_case_ids(path: Path) -> list[str]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    case_ids = [record["case_id"] for record in records]
    assert case_ids == sorted(case_ids)
    return case_ids


def test_interrupt_saves_sorted_files_and_resume_matches_full_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    titles = ["Bump requests from 2.31.0 to 2.32.0"] * 3

    full_out, full_code = _mine_into(
        tmp_path, FakeMulti(titles), out_name="full", cache_name="cache_full"
    )
    candidates_bytes = (full_out / "candidates.jsonl").read_bytes()
    excluded_bytes = (full_out / "excluded.jsonl").read_bytes()
    full_summary = json.loads((full_out / "summary.json").read_text(encoding="utf-8"))
    assert full_code == 0

    part_out, part_code = _mine_into(
        tmp_path,
        FakeMulti(titles, interrupt_at=2),
        out_name="part",
        cache_name="cache_part",
    )
    assert part_code == 130
    err = capsys.readouterr().err
    assert "interrupted; saved 1 candidate(s) and 0 exclusion(s)" in err
    assert str(part_out) in err
    assert _sorted_case_ids(part_out / "candidates.jsonl") == [f"{REPO_FULL}#1"]
    assert _sorted_case_ids(part_out / "excluded.jsonl") == []
    assert (part_out / "summary.json").exists()
    assert {path.name for path in part_out.iterdir()} == {
        "candidates.jsonl",
        "excluded.jsonl",
        "summary.json",
    }

    _, resumed = _mine_into(tmp_path, FakeMulti(titles), out_name="part", cache_name="cache_part")
    assert resumed == 0
    assert (part_out / "candidates.jsonl").read_bytes() == candidates_bytes
    assert (part_out / "excluded.jsonl").read_bytes() == excluded_bytes
    resumed_ids = _sorted_case_ids(part_out / "candidates.jsonl")
    assert len(resumed_ids) == len(set(resumed_ids)) == 3

    part_summary = json.loads((part_out / "summary.json").read_text(encoding="utf-8"))
    for key in ("kept", "excluded", "distinct_repos", "labels", "reasons", "bots", "splits"):
        assert part_summary[key] == full_summary[key]


def test_write_jsonl_retries_replace_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "candidates.jsonl"
    attempts: list[int] = []
    sleeps: list[float] = []
    real_replace = os.replace

    def flaky_replace(src: Path, dst: Path) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError("locked")
        real_replace(src, dst)

    monkeypatch.setattr(bench_mine, "_replace", flaky_replace)
    monkeypatch.setattr(bench_mine, "_replace_sleep", sleeps.append)

    bench_mine._write_jsonl(path, {"acme/widgets#1": {"case_id": "acme/widgets#1"}})

    assert len(attempts) == 2
    assert sleeps == [0.2]
    assert json.loads(path.read_text(encoding="utf-8"))["case_id"] == "acme/widgets#1"
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_jsonl_keyboard_interrupt_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "candidates.jsonl"
    previous = '{"case_id": "old"}\n'
    path.write_text(previous, encoding="utf-8")

    def interrupted_replace(src: Path, dst: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(bench_mine, "_replace", interrupted_replace)
    monkeypatch.setattr(bench_mine, "_replace_sleep", lambda seconds: None)

    with pytest.raises(KeyboardInterrupt):
        bench_mine._write_jsonl(path, {"acme/widgets#2": {"case_id": "acme/widgets#2"}})

    assert path.read_text(encoding="utf-8") == previous
    assert list(tmp_path.glob("*.tmp")) == []
