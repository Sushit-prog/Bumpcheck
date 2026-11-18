from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bumpcheck.bench.github import (
    USER_AGENT,
    GitHubClient,
    GitHubError,
    HttpStatusError,
    OfflineCacheMiss,
)


class Recorder:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    def __call__(self, method: str, url: str, headers: dict) -> tuple:
        self.calls.append({"method": method, "url": url, "headers": dict(headers)})
        route = self.routes.get(url)
        if route is None:
            raise AssertionError(f"unexpected URL: {url}")
        if callable(route):
            return route(method, url, dict(headers))
        return route


def _client(recorder: Recorder, tmp_path: Path, **kwargs) -> GitHubClient:
    sleeps: list[float] = kwargs.pop("sleeps", [])
    warns: list[str] = kwargs.pop("warns", [])
    clock = {"now": 1000.0}

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    return GitHubClient(
        token=kwargs.pop("token", "test-token"),
        transport=recorder,
        sleep=sleep,
        now=kwargs.pop("now", lambda: clock["now"]),
        rng=kwargs.pop("rng", lambda: 0.0),
        warn=kwargs.pop("warn", warns.append),
        cache_dir=kwargs.pop("cache_dir", tmp_path / "cache"),
        **kwargs,
    )


def _json(payload: object) -> tuple[int, dict, bytes]:
    return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")


def test_token_is_sent_but_never_written_to_cache(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    recorder = Recorder({url: _json({"ok": True})})
    client = _client(recorder, tmp_path, token="super-secret-token")

    assert client.get_json(url) == {"ok": True}

    assert recorder.calls[0]["headers"]["Authorization"] == "Bearer super-secret-token"
    cache_files = list((tmp_path / "cache").glob("*.json"))
    assert len(cache_files) == 1
    for cache_file in cache_files:
        text = cache_file.read_text(encoding="utf-8")
        assert "super-secret-token" not in text
        assert "Authorization" not in text


def test_etag_revalidation_counts_as_cache_hit(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    seen_conditionals: list[str | None] = []

    def route(method: str, url: str, headers: dict) -> tuple:
        seen_conditionals.append(headers.get("If-None-Match"))
        if headers.get("If-None-Match") == '"v1"':
            return 304, {"ETag": '"v1"'}, b""
        return 200, {"ETag": '"v1"'}, b'{"ok": true}'

    recorder = Recorder({url: route})
    client = _client(recorder, tmp_path)

    assert client.get_json(url) == {"ok": True}
    assert client.get_json(url) == {"ok": True}

    assert seen_conditionals == [None, '"v1"']
    assert client.api_calls == 2
    assert client.cache_hits == 1


def test_offline_uses_cache_without_transport(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    first = _client(Recorder({url: _json({"ok": True})}), tmp_path)
    assert first.get_json(url) == {"ok": True}

    recorder = Recorder({})
    offline = _client(recorder, tmp_path, offline=True)
    assert offline.get_json(url) == {"ok": True}
    assert recorder.calls == []
    assert offline.cache_hits == 1
    assert offline.api_calls == 0


def test_offline_cold_cache_raises(tmp_path: Path) -> None:
    client = _client(Recorder({}), tmp_path, offline=True)
    with pytest.raises(OfflineCacheMiss):
        client.get_bytes("https://api.github.com/repos/o/r")


def test_exhausted_rate_limit_sleeps_until_reset(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    responses = [
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1060"}, b""),
        (200, {"X-RateLimit-Remaining": "10"}, b'{"ok": true}'),
    ]
    calls = {"count": 0}

    def route(method: str, url: str, headers: dict) -> tuple:
        calls["count"] += 1
        return responses[min(calls["count"], len(responses)) - 1]

    sleeps: list[float] = []
    client = _client(Recorder({url: route}), tmp_path, sleeps=sleeps)

    assert client.get_json(url) == {"ok": True}
    assert sleeps == [60.5]


def test_retry_after_is_honoured(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    responses = [
        (429, {"Retry-After": "7"}, b""),
        (200, {"X-RateLimit-Remaining": "10"}, b'{"ok": true}'),
    ]
    calls = {"count": 0}

    def route(method: str, url: str, headers: dict) -> tuple:
        calls["count"] += 1
        return responses[min(calls["count"], len(responses)) - 1]

    sleeps: list[float] = []
    client = _client(Recorder({url: route}), tmp_path, sleeps=sleeps)

    assert client.get_json(url) == {"ok": True}
    assert sleeps == [7.0]


def test_server_errors_back_off_and_then_fail(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    recorder = Recorder({url: (500, {}, b"boom")})
    sleeps: list[float] = []
    client = _client(recorder, tmp_path, sleeps=sleeps)

    with pytest.raises(HttpStatusError) as excinfo:
        client.get_json(url)

    assert excinfo.value.status == 500
    assert len(recorder.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_network_errors_are_retried_then_succeed(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    attempts = {"count": 0}

    def route(method: str, url: str, headers: dict) -> tuple:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise http.client.IncompleteRead(b"partial", 10)
        return _json({"ok": True})

    sleeps: list[float] = []
    client = _client(Recorder({url: route}), tmp_path, sleeps=sleeps)

    assert client.get_json(url) == {"ok": True}
    assert attempts["count"] == 3
    assert sleeps == [1.0, 2.0]


def test_network_errors_give_up_after_five_attempts(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"

    def route(method: str, url: str, headers: dict) -> tuple:
        raise urllib.error.URLError("offline")

    sleeps: list[float] = []
    recorder = Recorder({url: route})
    client = _client(recorder, tmp_path, sleeps=sleeps)

    with pytest.raises(GitHubError) as excinfo:
        client.get_json(url)

    message = str(excinfo.value)
    assert "Network error after 5 attempts for /repos/o/r" in message
    assert "URLError" in message
    assert "offline" in message
    assert "test-token" not in message
    assert len(recorder.calls) == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_transport_http_error_becomes_http_status_error(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"

    def route(method: str, url: str, headers: dict) -> tuple:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    client = _client(Recorder({url: route}), tmp_path)

    with pytest.raises(HttpStatusError) as excinfo:
        client.get_json(url)

    assert excinfo.value.status == 404
    assert excinfo.value.body == b""


def test_search_requests_are_throttled(tmp_path: Path) -> None:
    first = "https://api.github.com/search/issues?q=a&page=1"
    second = "https://api.github.com/search/issues?q=b&page=1"
    recorder = Recorder({first: _json({}), second: _json({})})
    sleeps: list[float] = []
    client = _client(recorder, tmp_path, sleeps=sleeps)

    client.get_json(first)
    client.get_json(second)

    assert sleeps == [3.0]


def test_job_log_redirect_has_no_authorization_header(tmp_path: Path) -> None:
    jobs_url = "https://api.github.com/repos/o/r/actions/jobs/9/logs"
    blob_url = "https://blob.example/9"
    recorder = Recorder(
        {
            jobs_url: (302, {"Location": blob_url}, b""),
            blob_url: (200, {"Content-Type": "text/plain"}, b"line one\nline two\n"),
        }
    )
    client = _client(recorder, tmp_path)

    assert client.get_log(jobs_url) == "line one\nline two\n"

    assert recorder.calls[0]["headers"]["Authorization"] == "Bearer test-token"
    assert "Authorization" not in recorder.calls[1]["headers"]
    assert recorder.calls[1]["url"] == blob_url


def test_job_logs_require_a_token(tmp_path: Path) -> None:
    client = _client(Recorder({}), tmp_path, token="")
    with pytest.raises(GitHubError):
        client.get_log("https://api.github.com/repos/o/r/actions/jobs/9/logs")


def test_http_error_status_is_exposed(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    client = _client(Recorder({url: (404, {}, b"not found")}), tmp_path)
    with pytest.raises(HttpStatusError) as excinfo:
        client.get_bytes(url)
    assert excinfo.value.status == 404


def _repeating(routes: list[tuple]) -> tuple:
    calls = {"count": 0}

    def route(method: str, url: str, headers: dict) -> tuple:
        calls["count"] += 1
        return routes[min(calls["count"], len(routes)) - 1]

    return route


def test_retry_after_is_used_unmodified_on_each_retry(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    route = _repeating(
        [
            (429, {"Retry-After": "7"}, b""),
            (429, {"Retry-After": "7"}, b""),
            (200, {"X-RateLimit-Remaining": "10"}, b'{"ok": true}'),
        ]
    )
    sleeps: list[float] = []
    warns: list[str] = []
    client = _client(Recorder({url: route}), tmp_path, sleeps=sleeps, warns=warns)

    assert client.get_json(url) == {"ok": True}
    assert sleeps == [7.0, 7.0]
    assert warns == [
        "rate limited (retry-after); sleeping 7s, retry 1/4 for /repos/o/r",
        "rate limited (retry-after); sleeping 7s, retry 2/4 for /repos/o/r",
    ]


def test_rate_limit_reset_wait_is_not_doubled_on_retry(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    route = _repeating(
        [
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1120"}, b""),
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1120"}, b""),
            (200, {"X-RateLimit-Remaining": "10"}, b'{"ok": true}'),
        ]
    )
    sleeps: list[float] = []
    client = _client(Recorder({url: route}), tmp_path, sleeps=sleeps)

    assert client.get_json(url) == {"ok": True}
    assert sleeps[:2] == [120.5, 0.5]


def test_secondary_rate_limit_403_backs_off_with_doubling(tmp_path: Path) -> None:
    url = "https://api.github.com/search/issues?q=x"
    body = json.dumps(
        {"message": "You have exceeded a secondary rate limit. Please wait and try again."}
    ).encode("utf-8")
    recorder = Recorder({url: (403, {}, body)})
    sleeps: list[float] = []
    warns: list[str] = []
    client = _client(recorder, tmp_path, sleeps=sleeps, warns=warns)

    with pytest.raises(HttpStatusError) as excinfo:
        client.get_json(url)

    assert len(recorder.calls) == 5
    assert sleeps == [60.0, 120.0, 240.0, 480.0]
    assert "secondary rate limit" in str(excinfo.value)
    assert warns == [
        "rate limited (secondary-rate-limit); sleeping 60s, retry 1/4 for /search/issues",
        "rate limited (secondary-rate-limit); sleeping 120s, retry 2/4 for /search/issues",
        "rate limited (secondary-rate-limit); sleeping 240s, retry 3/4 for /search/issues",
        "rate limited (secondary-rate-limit); sleeping 480s, retry 4/4 for /search/issues",
    ]
    assert all("test-token" not in line for line in warns)


def test_abuse_detection_403_is_retryable(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    body = json.dumps({"message": "You have triggered an abuse detection mechanism."}).encode(
        "utf-8"
    )
    route = _repeating([(403, {}, body), (200, {}, b'{"ok": true}')])
    sleeps: list[float] = []
    warns: list[str] = []
    client = _client(Recorder({url: route}), tmp_path, sleeps=sleeps, warns=warns)

    assert client.get_json(url) == {"ok": True}
    assert sleeps == [60.0]
    assert warns == ["rate limited (abuse-detection); sleeping 60s, retry 1/4 for /repos/o/r"]


def test_plain_403_fails_immediately_with_message(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    body = json.dumps({"message": "Resource not accessible by integration"}).encode("utf-8")
    recorder = Recorder({url: (403, {}, body)})
    sleeps: list[float] = []
    warns: list[str] = []
    client = _client(recorder, tmp_path, sleeps=sleeps, warns=warns)

    with pytest.raises(HttpStatusError) as excinfo:
        client.get_json(url)

    assert len(recorder.calls) == 1
    assert sleeps == []
    assert warns == []
    assert "Resource not accessible by integration" in str(excinfo.value)


def test_rate_limit_headers_appear_in_error_but_never_the_token(tmp_path: Path) -> None:
    url = "https://api.github.com/repos/o/r"
    headers = {
        "X-RateLimit-Remaining": "1",
        "X-RateLimit-Reset": "1234",
        "X-RateLimit-Resource": "core",
    }
    body = json.dumps({"message": "Resource not accessible by integration"}).encode("utf-8")
    client = _client(Recorder({url: (403, headers, body)}), tmp_path, token="super-secret-token")

    with pytest.raises(HttpStatusError) as excinfo:
        client.get_bytes(url)

    text = str(excinfo.value)
    assert "Resource not accessible by integration" in text
    assert "x-ratelimit-remaining=1" in text
    assert "x-ratelimit-reset=1234" in text
    assert "x-ratelimit-resource=core" in text
    assert "super-secret-token" not in text
    assert "Authorization" not in text


def test_minimum_request_gap_applies_to_all_requests(tmp_path: Path) -> None:
    first = "https://api.github.com/repos/o/a"
    second = "https://api.github.com/repos/o/b"
    recorder = Recorder({first: _json({}), second: _json({})})
    sleeps: list[float] = []
    client = _client(recorder, tmp_path, sleeps=sleeps)

    client.get_json(first)
    client.get_json(second)

    assert sleeps == [1.0]


def test_external_requests_carry_no_authorization_including_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, dict[str, str]]] = []
    redirector = urllib.request.HTTPRedirectHandler()

    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_urlopen(
        request: urllib.request.Request, timeout: float | None = None, **kwargs: object
    ) -> _Response:
        captured.append((request.full_url, dict(request.header_items())))
        if len(captured) == 1:
            hop = redirector.redirect_request(
                request, None, 302, "Moved", {}, "https://files.pythonhosted.org/final.whl"
            )
            assert hop is not None
            return fake_urlopen(hop, timeout=timeout)
        return _Response(b'{"info": {}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = GitHubClient(token="super-secret-token", cache_dir=tmp_path / "cache")
    body = client.get_external_bytes("https://pypi.org/pypi/requests/2.32.0/json")

    assert body == b'{"info": {}}'
    assert client.token == "super-secret-token"
    assert [url for url, _ in captured] == [
        "https://pypi.org/pypi/requests/2.32.0/json",
        "https://files.pythonhosted.org/final.whl",
    ]
    for _, headers in captured:
        assert "authorization" not in {name.lower() for name in headers}
        assert all("super-secret-token" not in value for value in headers.values())
    first_headers = captured[0][1]
    user_agents = [value for name, value in first_headers.items() if name.lower() == "user-agent"]
    assert user_agents == [USER_AGENT]
