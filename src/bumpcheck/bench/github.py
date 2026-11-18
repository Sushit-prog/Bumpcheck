"""Minimal GitHub REST client with throttling and an on-disk response cache.

The client never logs the token, never writes it to the cache, and never
sends it to redirect targets. All sleeps and the transport are injectable so
tests run offline and without real delays.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com"
RAW_ROOT = "https://raw.githubusercontent.com"
USER_AGENT = "bumpcheck-bench/0.1"
DEFAULT_CACHE_DIR = Path(".bumpcheck-cache") / "github"
SEARCH_MIN_INTERVAL = 3.0
MIN_REQUEST_INTERVAL = 1.0
MAX_ATTEMPTS = 3
MAX_RATE_LIMIT_RETRIES = 4
MAX_NET_RETRIES = 4
NOTICE_MIN_SLEEP = 5.0
REQUEST_TIMEOUT = 30

RETRYABLE_NETWORK_ERRORS = (
    http.client.IncompleteRead,
    urllib.error.URLError,
    TimeoutError,
    ConnectionResetError,
    ssl.SSLError,
)

RATE_LIMIT_HEADERS = (
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-ratelimit-resource",
    "retry-after",
)

Transport = Callable[[str, str, Mapping[str, str]], tuple[int, Mapping[str, str], bytes]]


class GitHubError(Exception):
    """A GitHub request failed and could not be recovered."""


class GitHubFatalError(GitHubError):
    """A GitHub error that callers must not downgrade to a soft failure."""


class GitHubAuthError(GitHubFatalError):
    """GitHub rejected the configured token."""


class RateLimitWaitTooLong(GitHubFatalError):
    """The next rate-limit wait exceeds the configured maximum."""


class HttpStatusError(GitHubError):
    """A GitHub request returned a non-retryable error status."""

    def __init__(
        self,
        status: int,
        url: str,
        body: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.url = url
        self.body = body
        self.headers = dict(headers or {})
        detail = f"GitHub returned HTTP {status} for {url}"
        message = _error_message(body)
        if message:
            detail = f"{detail}: {message}"
        extras = [
            f"{name}={value}"
            for name in RATE_LIMIT_HEADERS
            if (value := _header(self.headers, name)) is not None
        ]
        if extras:
            detail = f"{detail} ({', '.join(extras)})"
        super().__init__(detail)


class OfflineCacheMiss(GitHubError):
    """Offline mode was requested but a URL is not in the cache."""


def _stderr_warning(message: str) -> None:
    print(message, file=sys.stderr)


def _local_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") + " local"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str]
) -> tuple[int, Mapping[str, str], bytes]:
    """Default transport: one request, redirects returned to the caller."""
    request = urllib.request.Request(url, headers=dict(headers), method=method)
    try:
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, body


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _error_message(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return None


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


class GitHubClient:
    """GET-only GitHub client with rate limiting, retries, and a URL cache."""

    def __init__(
        self,
        *,
        token: str | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        warn: Callable[[str], None] | None = None,
        cache_dir: Path | None = None,
        offline: bool = False,
        max_rate_limit_wait_s: float = 900.0,
    ) -> None:
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._now = now
        self._rng = rng
        self._warn = warn if warn is not None else _stderr_warning
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        self.offline = offline
        self.max_rate_limit_wait_s = max_rate_limit_wait_s
        self.api_calls = 0
        self.search_calls = 0
        self.cache_hits = 0
        self._last_request_at: float | None = None
        self._pending_reset_at: float | None = None

    def _base_headers(self, *, authenticate: bool = True) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if authenticate and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_json(self, url: str) -> Any:
        body = self.get_bytes(url)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GitHubError(f"Invalid JSON response for {url}") from exc

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8-sig")

    def get_bytes(self, url: str) -> bytes:
        entry = self._load_cache(url)
        if self.offline:
            if entry is None:
                raise OfflineCacheMiss(
                    f"--offline was given but nothing for {url} is cached in {self.cache_dir}"
                )
            self.cache_hits += 1
            return entry["body"]

        headers = self._base_headers()
        if entry is not None and entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        status, response_headers, body = self._send(url, headers)
        if status == 304 and entry is not None:
            self.cache_hits += 1
            self._pause_if_exhausted(response_headers)
            return entry["body"]
        if status >= 400:
            raise HttpStatusError(status, url, body, response_headers)
        if status == 200:
            self._store_cache(url, _header(response_headers, "etag"), body)
        self._pause_if_exhausted(response_headers)
        return body

    def get_external_bytes(self, url: str, *, timeout: float = 10) -> bytes:
        """Fetch a non-GitHub URL through the same cache, without credentials."""
        entry = self._load_cache(url)
        if entry is not None:
            self.cache_hits += 1
            return entry["body"]
        if self.offline:
            raise OfflineCacheMiss(
                f"--offline was given but nothing for {url} is cached in {self.cache_dir}"
            )
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp is not None else b""
            headers = dict(exc.headers.items()) if exc.headers else {}
            raise HttpStatusError(exc.code, url, body, headers) from exc
        except RETRYABLE_NETWORK_ERRORS as exc:
            path = urllib.parse.urlsplit(url).path
            raise GitHubError(f"Network error for {path}: {type(exc).__name__}: {exc}") from exc
        self._store_cache(url, None, body)
        return body

    def get_external_json(self, url: str, *, timeout: float = 10) -> Any:
        body = self.get_external_bytes(url, timeout=timeout)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            path = urllib.parse.urlsplit(url).path
            raise GitHubError(f"Invalid JSON response for {path}") from exc

    def get_log(self, url: str) -> str:
        """Fetch a job log, following its redirect without the token.

        GitHub answers ``/actions/jobs/{id}/logs`` with a 302 to a short-lived
        blob URL that rejects an Authorization header, so the redirect target
        is fetched separately with no credentials.
        """
        if not self.token:
            raise GitHubError("GitHub job logs require GITHUB_TOKEN")
        entry = self._load_cache(url)
        if self.offline:
            if entry is None:
                raise OfflineCacheMiss(
                    f"--offline was given but nothing for {url} is cached in {self.cache_dir}"
                )
            self.cache_hits += 1
            return entry["body"].decode("utf-8", "replace")
        if entry is not None:
            return entry["body"].decode("utf-8", "replace")

        status, headers, body = self._send(url, self._base_headers())
        if 300 <= status < 400:
            location = _header(headers, "location")
            if not location:
                raise GitHubError(f"Redirect without Location for {url}")
            status, headers, body = self._send(location, self._base_headers(authenticate=False))
        if status >= 400:
            raise HttpStatusError(status, url, body, headers)
        self._store_cache(url, None, body)
        self._pause_if_exhausted(headers)
        return body.decode("utf-8", "replace")

    def _send(self, url: str, headers: dict[str, str]) -> tuple[int, Mapping[str, str], bytes]:
        self._wait_for_pending_reset()
        attempt = 0
        rate_limit_attempt = 0
        net_attempt = 0
        while True:
            self._throttle(url)
            try:
                status, response_headers, body = self._transport("GET", url, headers)
            except urllib.error.HTTPError as exc:
                body = exc.read() if exc.fp is not None else b""
                status = exc.code
                response_headers = dict(exc.headers.items()) if exc.headers else {}
            except RETRYABLE_NETWORK_ERRORS as exc:
                if net_attempt >= MAX_NET_RETRIES:
                    path = urllib.parse.urlsplit(url).path
                    raise GitHubError(
                        f"Network error after {MAX_NET_RETRIES + 1} attempts for {path}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                self._sleep((2.0**net_attempt) + self._rng() * 0.5)
                net_attempt += 1
                continue
            self.api_calls += 1
            if "/search/" in url:
                self.search_calls += 1
            if status in {403, 429}:
                rate = self._rate_limit_sleep(response_headers, body, rate_limit_attempt)
                if rate is not None and rate_limit_attempt < MAX_RATE_LIMIT_RETRIES:
                    delay, reason = rate
                    rate_limit_attempt += 1
                    wake = self._rate_limit_wake(response_headers, delay)
                    if delay > self.max_rate_limit_wait_s:
                        raise RateLimitWaitTooLong(self._wait_too_long_message(delay, wake))
                    if delay >= NOTICE_MIN_SLEEP:
                        path = urllib.parse.urlsplit(url).path
                        self._warn(
                            f"rate limited ({reason}); sleeping {delay:g}s until {wake}, "
                            f"retry {rate_limit_attempt}/{MAX_RATE_LIMIT_RETRIES} for {path}"
                        )
                    self._sleep(delay)
                    continue
                return status, response_headers, body
            if status == 401 and _header(headers, "authorization") is not None:
                raise GitHubAuthError(
                    "GitHub rejected the token (401): it is missing, expired or revoked"
                )
            if 500 <= status < 600 and attempt + 1 < MAX_ATTEMPTS:
                self._sleep((2.0**attempt) + self._rng() * 0.5)
                attempt += 1
                continue
            return status, response_headers, body

    def _wait_too_long_message(self, delay: float, wake: str) -> str:
        return (
            f"rate limit wait is {delay / 60:.1f} minutes (until {wake}); "
            "re-run after that time, or pass --max-rate-limit-wait SECONDS to wait"
        )

    def _rate_limit_wake(self, headers: Mapping[str, str], delay: float) -> str:
        reset = _header(headers, "x-ratelimit-reset")
        if reset is not None:
            try:
                return _local_time(float(reset))
            except ValueError:
                pass
        return _local_time(self._now() + delay)

    def _rate_limit_sleep(
        self, headers: Mapping[str, str], body: bytes, attempt: int
    ) -> tuple[float, str] | None:
        retry_after = _header(headers, "retry-after")
        remaining = _header(headers, "x-ratelimit-remaining")
        reset = _header(headers, "x-ratelimit-reset")
        message = (_error_message(body) or "").lower()
        mentions_limit = "rate limit" in message
        mentions_abuse = "abuse" in message
        if not (retry_after is not None or remaining == "0" or mentions_limit or mentions_abuse):
            return None
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after)), "retry-after"
            except ValueError:
                pass
        if remaining == "0" and reset is not None:
            try:
                reset_at = float(reset)
            except ValueError:
                pass
            else:
                return max(0.0, reset_at - self._now()) + 0.5, "rate-limit-reset"
        reason = "secondary-rate-limit" if mentions_limit else "abuse-detection"
        if not mentions_limit and not mentions_abuse:
            reason = "rate-limit"
        return 60.0 * (2.0**attempt), reason

    def _pause_if_exhausted(self, headers: Mapping[str, str]) -> None:
        if _header(headers, "x-ratelimit-remaining") != "0":
            return
        reset = _header(headers, "x-ratelimit-reset")
        try:
            self._pending_reset_at = float(reset) if reset is not None else self._now() + 60.0
        except ValueError:
            self._pending_reset_at = self._now() + 60.0

    def _wait_for_pending_reset(self) -> None:
        reset_at = self._pending_reset_at
        if reset_at is None:
            return
        self._pending_reset_at = None
        delay = max(0.0, reset_at - self._now()) + 0.5
        wake = _local_time(reset_at)
        if delay > self.max_rate_limit_wait_s:
            raise RateLimitWaitTooLong(self._wait_too_long_message(delay, wake))
        self._sleep(delay)

    def _throttle(self, url: str) -> None:
        interval = SEARCH_MIN_INTERVAL if "/search/" in url else MIN_REQUEST_INTERVAL
        now = self._now()
        if self._last_request_at is not None:
            wait = interval - (now - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
                now += wait
        self._last_request_at = now

    def _cache_file(self, url: str) -> Path:
        return self.cache_dir / f"{_cache_key(url)}.json"

    def _load_cache(self, url: str) -> dict[str, Any] | None:
        path = self._cache_file(url)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("url") != url:
            return None
        body = payload.get("body")
        if not isinstance(body, str):
            return None
        try:
            decoded = base64.b64decode(body.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return None
        return {"url": url, "etag": payload.get("etag"), "body": decoded}

    def _store_cache(self, url: str, etag: str | None, body: bytes) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "url": url,
            "etag": etag,
            "body": base64.b64encode(body).decode("ascii"),
        }
        self._cache_file(url).write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
        )
