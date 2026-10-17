"""Polite, resilient HTTP layer: browser identities, adaptive pacing and block recovery."""

from .client import BlockedError, CancelledError, FetchError, HttpClient, NotFoundError, TrafficStats
from .identity import BrowserIdentity, IdentityFactory
from .ratelimit import AdaptiveRateLimiter

__all__ = [
    "AdaptiveRateLimiter",
    "BlockedError",
    "BrowserIdentity",
    "CancelledError",
    "FetchError",
    "HttpClient",
    "IdentityFactory",
    "NotFoundError",
    "TrafficStats",
]
