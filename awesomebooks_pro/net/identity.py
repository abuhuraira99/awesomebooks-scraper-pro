"""Browser identities.

An identity is everything a server can use to tell visitors apart: the TLS /
HTTP2 fingerprint, the User-Agent and client hints, the language preference and
the cookie jar. Each worker session owns one identity at a time and retires it
after a random number of requests (or immediately after any warning sign), so
the traffic looks like a series of ordinary, short-lived shoppers instead of a
single tireless client.

When ``curl_cffi`` is installed the TLS and HTTP/2 handshakes are byte-for-byte
those of the real browser named by ``impersonate``; otherwise we fall back to
``requests`` with matching headers.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BrowserProfile:
    impersonate: str  # curl_cffi target
    user_agent: str  # used by the requests fallback
    label: str


# Only current desktop browsers: the same families real AwesomeBooks shoppers use.
PROFILES: tuple[BrowserProfile, ...] = (
    BrowserProfile(
        "chrome146",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Chrome 146 · Windows",
    ),
    BrowserProfile(
        "chrome145",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Chrome 145 · macOS",
    ),
    BrowserProfile(
        "chrome142",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Chrome 142 · Windows",
    ),
    BrowserProfile(
        "chrome150",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "Chrome 150 · Windows",
    ),
    BrowserProfile(
        "safari260",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
        "Safari 26 · macOS",
    ),
    BrowserProfile(
        "safari184",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
        "Safari 18.4 · macOS",
    ),
    BrowserProfile(
        "firefox147",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
        "Firefox 147 · Windows",
    ),
    BrowserProfile(
        "firefox144",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:144.0) Gecko/20100101 Firefox/144.0",
        "Firefox 144 · macOS",
    ),
)

ACCEPT_LANGUAGES: tuple[str, ...] = (
    "en-US,en;q=0.9",
    "en-US,en;q=0.9",
    "en-US,en;q=0.8",
    "en-CA,en-US;q=0.9,en;q=0.8",
    "en-GB,en-US;q=0.9,en;q=0.8",
    "en-US,en;q=0.9,es;q=0.7",
    "en-US,en;q=0.9,fr-CA;q=0.6",
)


@dataclass
class BrowserIdentity:
    profile: BrowserProfile
    accept_language: str
    proxy: str | None
    request_budget: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    requests_made: int = 0

    @property
    def exhausted(self) -> bool:
        return self.requests_made >= self.request_budget

    def base_headers(self) -> dict[str, str]:
        return {"Accept-Language": self.accept_language}

    def describe(self) -> str:
        via = " via proxy" if self.proxy else ""
        return f"{self.profile.label}{via} [{self.id}]"


class IdentityFactory:
    """Creates fresh identities, round-robining through proxies when configured."""

    def __init__(
        self,
        proxies: list[str] | None = None,
        rng: random.Random | None = None,
        budget_range: tuple[int, int] = (35, 90),
    ) -> None:
        self._proxies = list(proxies or [])
        self._rng = rng or random.Random()
        self._budget_range = budget_range
        self._proxy_cursor = self._rng.randrange(len(self._proxies)) if self._proxies else 0
        self._last_profile: str | None = None

    @property
    def has_proxies(self) -> bool:
        return bool(self._proxies)

    def create(self) -> BrowserIdentity:
        choices = [p for p in PROFILES if p.impersonate != self._last_profile] or list(PROFILES)
        profile = self._rng.choice(choices)
        self._last_profile = profile.impersonate
        proxy = None
        if self._proxies:
            proxy = self._proxies[self._proxy_cursor % len(self._proxies)]
            self._proxy_cursor += 1
        return BrowserIdentity(
            profile=profile,
            accept_language=self._rng.choice(ACCEPT_LANGUAGES),
            proxy=proxy,
            request_budget=self._rng.randint(*self._budget_range),
        )
