"""The HTTP client every worker session uses.

Responsibilities:

* wait for a slot from the shared :class:`AdaptiveRateLimiter`;
* send the request with the session's current :class:`BrowserIdentity`;
* classify the answer: success, "not found", throttled/blocked or transient;
* on warnings, cool everyone down, retire the identity and retry;
* never turn a network failure into a data answer. Callers get either real
  data, :class:`NotFoundError` (the site said 404) or an exception.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests as _requests

from .identity import BrowserIdentity, IdentityFactory
from .ratelimit import AdaptiveRateLimiter

log = logging.getLogger(__name__)

CURL_CFFI_ERROR: str | None = None
try:  # pragma: no cover - import guard
    from curl_cffi import requests as _curl_requests

    HAS_CURL_CFFI = True
except Exception as _exc:  # pragma: no cover - fallback path (e.g. DLL blocked, path too long)
    _curl_requests = None
    HAS_CURL_CFFI = False
    CURL_CFFI_ERROR = f"{type(_exc).__name__}: {_exc}"

THROTTLE_STATUSES = {408, 429, 430, 503, 520, 521, 522, 523, 524, 525, 526, 527, 529}
# Unmistakable bot-challenge pages. Only these can turn an HTTP 200 into a "blocked" verdict.
# Note: every normal AwesomeBooks page loads Cloudflare's passive detection script from
# "/cdn-cgi/challenge-platform/scripts/jsd/main.js", so the bare word "challenge-platform" is NOT
# a challenge. Interstitials use the "/challenge-platform/h/" orchestrator and "cf-chl" tokens.
CHALLENGE_MARKERS = (
    "cf-chl",
    "_cf_chl_opt",
    "/cdn-cgi/challenge-platform/h/",
    "challenges.cloudflare.com/turnstile",
    "<title>just a moment...</title>",
    "attention required! | cloudflare",
    "please verify you are a human",
    "verify you are human",
)
# Wording that only means "blocked" on an error status (a normal page may mention captcha scripts).
DENIAL_MARKERS = (
    *CHALLENGE_MARKERS,
    "just a moment",
    "captcha",
    "access denied",
    "request blocked",
    "attention required",
    "you have been blocked",
    "error code: 1015",  # Cloudflare rate limiting
    "error 1020",  # Cloudflare firewall rule
)


# Failures that mean *this machine* lost connectivity, not that the site refused us.
OFFLINE_MARKERS = (
    "could not resolve host",
    "couldn't resolve host",
    "getaddrinfo failed",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "failed to connect",
    "couldn't connect",
    "network is unreachable",
    "no route to host",
    "dnserror",
    "nameresolutionerror",
    "failed to establish a new connection",
    "winerror 10051",  # network unreachable
    "winerror 10065",  # host unreachable
    "winerror 11001",  # host not found
    "winerror 11004",  # no data record
)
PROBE_HOSTS = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443))


def _is_offline_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in OFFLINE_MARKERS)


def internet_reachable(timeout: float = 3.0) -> bool:
    """True if *any* well-known public host answers: tells "our internet is down" from "the site refuses us"."""
    for host, port in PROBE_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class FetchError(RuntimeError):
    """The request could not be completed after all retries."""


class NotFoundError(FetchError):
    """The site answered 404 — a definitive "does not exist"."""


class BlockedError(FetchError):
    """The site kept signalling throttling or a bot challenge."""


class CancelledError(RuntimeError):
    """The job was stopped while waiting for a request slot."""


class Response(Protocol):
    status_code: int
    text: str
    headers: Any


class Transport(Protocol):
    def get(self, url: str, *, params: dict | None, headers: dict, timeout: float) -> Response: ...

    def post(self, url: str, *, data: str, headers: dict, timeout: float) -> Response: ...

    def close(self) -> None: ...


class _CurlTransport:
    def __init__(self, identity: BrowserIdentity) -> None:
        proxies = {"http": identity.proxy, "https": identity.proxy} if identity.proxy else None
        self._session = _curl_requests.Session(impersonate=identity.profile.impersonate, proxies=proxies)

    def get(self, url: str, *, params: dict | None, headers: dict, timeout: float) -> Response:
        return self._session.get(url, params=params, headers=headers, timeout=timeout, allow_redirects=True)

    def post(self, url: str, *, data: str, headers: dict, timeout: float) -> Response:
        return self._session.post(url, data=data, headers=headers, timeout=timeout, allow_redirects=True)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass


class _RequestsTransport:
    def __init__(self, identity: BrowserIdentity) -> None:
        self._session = _requests.Session()
        self._session.headers.update(
            {
                "User-Agent": identity.profile.user_agent,
                # Only advertise encodings requests can decode (Brotli needs an extra package).
                "Accept-Encoding": "gzip, deflate",
            }
        )
        if identity.proxy:
            self._session.proxies.update({"http": identity.proxy, "https": identity.proxy})

    def get(self, url: str, *, params: dict | None, headers: dict, timeout: float) -> Response:
        return self._session.get(url, params=params, headers=headers, timeout=timeout)

    def post(self, url: str, *, data: str, headers: dict, timeout: float) -> Response:
        return self._session.post(url, data=data, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._session.close()


def default_transport_factory(identity: BrowserIdentity) -> Transport:
    if HAS_CURL_CFFI:
        return _CurlTransport(identity)
    return _RequestsTransport(identity)


@dataclass
class TrafficStats:
    """Thread-safe counters surfaced in the UI's Traffic Guard panel."""

    requests: int = 0
    successes: int = 0
    not_found: int = 0
    warnings: int = 0
    network_errors: int = 0
    identities: int = 0
    offline_events: int = 0
    offline_since: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + amount)

    def mark_offline(self) -> bool:
        """Returns True for the first session to notice the outage."""
        with self._lock:
            if self.offline_since is not None:
                return False
            self.offline_since = time.monotonic()
            self.offline_events += 1
            return True

    def mark_online(self) -> float | None:
        """Returns the outage duration if this call ended one."""
        with self._lock:
            if self.offline_since is None:
                return None
            duration = time.monotonic() - self.offline_since
            self.offline_since = None
            return duration

    def as_dict(self) -> dict[str, int]:
        with self._lock:
            return {
                "requests": self.requests,
                "successes": self.successes,
                "not_found": self.not_found,
                "warnings": self.warnings,
                "network_errors": self.network_errors,
                "identities": self.identities,
                "offline_events": self.offline_events,
                "offline_for": round(time.monotonic() - self.offline_since, 1) if self.offline_since is not None else 0,
            }


EventSink = Callable[[str, str], None]


def _is_challenge(text: str, markers: tuple[str, ...] = CHALLENGE_MARKERS) -> bool:
    head = text[:6000].lower()
    return any(marker in head for marker in markers)


def _retry_after(headers: Any) -> float | None:
    try:
        value = headers.get("Retry-After") if headers is not None else None
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class HttpClient:
    """One worker session. Not thread-safe: create one per worker thread."""

    def __init__(
        self,
        limiter: AdaptiveRateLimiter,
        identities: IdentityFactory,
        *,
        stats: TrafficStats | None = None,
        cancel: threading.Event | None = None,
        on_event: EventSink | None = None,
        timeout: float = 30.0,
        max_attempts: int = 7,
        transport_factory: Callable[[BrowserIdentity], Transport] = default_transport_factory,
        rng: random.Random | None = None,
        backoff_scale: float = 1.0,
        connectivity_check: Callable[[], bool] = internet_reachable,
    ) -> None:
        self.limiter = limiter
        self.identities = identities
        self.stats = stats or TrafficStats()
        self.cancel = cancel or threading.Event()
        self.on_event = on_event or (lambda level, message: None)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._transport_factory = transport_factory
        self.backoff_scale = backoff_scale
        self._connectivity_check = connectivity_check
        self._probe_at = -1e9
        self._probe_ok = True
        self._rng = rng or random.Random()
        self._identity: BrowserIdentity | None = None
        self._transport: Transport | None = None
        # Bumped on every new identity (fresh cookie jar). Site clients compare it to know when
        # per-visitor state, such as the chosen shipping country, has to be set up again.
        self.identity_serial = 0

    # -- identity management -------------------------------------------
    @property
    def identity(self) -> BrowserIdentity:
        if self._identity is None or self._identity.exhausted:
            self.rotate_identity()
        assert self._identity is not None
        return self._identity

    def rotate_identity(self, reason: str | None = None) -> None:
        if self._transport is not None:
            self._transport.close()
        self._identity = self.identities.create()
        self._transport = self._transport_factory(self._identity)
        self.identity_serial += 1
        self.stats.bump("identities")
        if reason:
            log.info("new browser identity %s after %s", self._identity.describe(), reason)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # -- public API -----------------------------------------------------
    def get_text(self, url: str, params: dict | None = None, *, referer: str | None = None) -> str:
        return self._fetch(url, params, referer=referer, expect_json=False)

    def get_json(
        self, url: str, params: dict | None = None, *, referer: str | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        return json.loads(self._fetch(url, params, referer=referer, expect_json=True, extra_headers=headers))

    def post_json(self, url: str, payload: Any, *, referer: str | None = None, headers: dict[str, str] | None = None) -> Any:
        """POST a JSON body (as the site's own scripts do) and decode the JSON answer."""
        extra = {"Content-Type": "application/json", **(headers or {})}
        body = json.dumps(payload, separators=(",", ":"))
        return json.loads(self._fetch(url, None, referer=referer, expect_json=True, extra_headers=extra, body=body))

    # -- core loop ------------------------------------------------------
    def _headers(
        self, identity: BrowserIdentity, referer: str | None, expect_json: bool, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        headers = identity.base_headers()
        if expect_json:
            headers["Accept"] = "application/json"
        else:
            headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        if referer:
            headers["Referer"] = referer
        if extra:
            headers.update(extra)
        return headers

    def _offline_confirmed(self) -> bool:
        """Is *this machine* offline? Re-checked on every wait (cached 15 s), so a site that keeps
        refusing us after the internet is back is treated as a site problem, never waited on forever."""
        if self.identities.has_proxies:
            return True  # traffic only leaves through the proxies, so a direct probe proves nothing
        now = time.monotonic()
        if now - self._probe_at > 15.0:
            self._probe_ok = self._connectivity_check()
            self._probe_at = now
        return not self._probe_ok

    def _sleep(self, seconds: float) -> None:
        if self.cancel.wait(seconds * self.backoff_scale):
            raise CancelledError("stopped")

    def _fetch(
        self,
        url: str,
        params: dict | None,
        *,
        referer: str | None,
        expect_json: bool,
        extra_headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> str:
        last_reason = "no attempt made"
        network_failures = 0
        offline_waits = 0
        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            if not self.limiter.acquire(self.cancel):
                raise CancelledError("stopped")
            identity = self.identity
            identity.requests_made += 1
            self.stats.bump("requests")
            try:
                assert self._transport is not None
                headers = self._headers(identity, referer, expect_json, extra_headers)
                if body is None:
                    resp = self._transport.get(url, params=params, headers=headers, timeout=self.timeout)
                else:
                    resp = self._transport.post(url, data=body, headers=headers, timeout=self.timeout)
            except Exception as exc:  # network level: DNS, reset, timeout, TLS...
                self.stats.bump("network_errors")
                if _is_offline_error(exc) and self._offline_confirmed():
                    # Our own connection is down: wait it out without spending the item's attempts.
                    attempt -= 1
                    offline_waits += 1
                    if self.stats.mark_offline():
                        self.on_event("warn", "Internet connection lost. Every session is waiting and will resume on its own.")
                    if self.identities.has_proxies:
                        self.rotate_identity()
                    self._sleep(min(60.0, 5.0 * offline_waits))
                    continue
                network_failures += 1
                last_reason = f"network error: {type(exc).__name__}: {str(exc)[:140]}"
                log.info("%s (%s) attempt %d", last_reason, url, attempt)
                if network_failures >= 2:
                    self.rotate_identity("repeated network errors")
                self._sleep(min(60.0, 2.0**attempt + self._rng.uniform(0, 1.5)))
                continue

            outage = self.stats.mark_online()
            if outage is not None:
                self.on_event("success", f"Connection restored after {outage:.0f}s. Resuming.")
            status = resp.status_code
            text = resp.text or ""

            if status == 404:
                self.limiter.record_success()
                self.stats.bump("not_found")
                raise NotFoundError(f"404 for {url}")

            # Behind Cloudflare a 403 is a firewall or bot verdict, never a "page not found".
            blocked = status in THROTTLE_STATUSES or status == 403 or (status == 401 and _is_challenge(text, DENIAL_MARKERS))
            if status == 200 and _is_challenge(text) and "<html" in text[:500].lower():
                blocked = True
            if status == 200 and expect_json and text.lstrip()[:1] not in ("{", "["):
                # JSON endpoint answered with HTML: almost always an interstitial page.
                blocked = True

            if blocked:
                self.stats.bump("warnings")
                cooldown = self.limiter.record_warning(_retry_after(resp.headers))
                last_reason = f"throttled (HTTP {status})"
                self.on_event(
                    "warn",
                    f"Site signalled HTTP {status}. All sessions paused {cooldown:.0f}s and pace slowed down.",
                )
                self.rotate_identity(f"HTTP {status}")
                continue

            if 500 <= status < 600:
                last_reason = f"server error HTTP {status}"
                self._sleep(min(45.0, 3.0 * attempt + self._rng.uniform(0, 2)))
                continue

            if status >= 400:
                raise FetchError(f"HTTP {status} for {url}")

            self.limiter.record_success()
            self.stats.bump("successes")
            return text

        if "throttled" in last_reason:
            raise BlockedError(f"gave up after {self.max_attempts} attempts: {last_reason}")
        raise FetchError(f"gave up after {self.max_attempts} attempts: {last_reason}")
