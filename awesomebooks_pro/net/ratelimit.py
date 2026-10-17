"""A global, adaptive request scheduler shared by every worker session.

Adding sessions never increases the request rate: all sessions draw from the
same schedule, so the site sees the pace the operator picked no matter how many
workers are running. On any throttling signal the scheduler

1. doubles the gap between requests (multiplicative decrease),
2. freezes *all* sessions for a cool-down (honouring ``Retry-After``), and
3. after repeated warnings trips a circuit breaker for a long pause.

A long streak of clean responses slowly relaxes the gap back towards the
chosen pace (additive-increase style), so a run finds the fastest rate the site
is comfortable with and stays under it.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class LimiterSnapshot:
    base_interval: float
    current_interval: float
    cooldown_remaining: float
    consecutive_warnings: int
    total_warnings: int
    breaker_trips: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "base_interval": round(self.base_interval, 2),
            "current_interval": round(self.current_interval, 2),
            "cooldown_remaining": round(self.cooldown_remaining, 1),
            "consecutive_warnings": self.consecutive_warnings,
            "total_warnings": self.total_warnings,
            "breaker_trips": self.breaker_trips,
        }


class AdaptiveRateLimiter:
    def __init__(
        self,
        base_interval: float,
        *,
        jitter: float = 0.35,
        max_interval: float = 20.0,
        warning_cooldown: float = 30.0,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 240.0,
        recover_after: int = 25,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.base_interval = base_interval
        self.jitter = jitter
        self.max_interval = max_interval
        self.warning_cooldown = warning_cooldown
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown = breaker_cooldown
        self.recover_after = recover_after
        self._clock = clock
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._interval = base_interval
        self._next_slot = 0.0
        self._cooldown_until = 0.0
        self._consecutive_warnings = 0
        self._clean_streak = 0
        self._total_warnings = 0
        self._breaker_trips = 0

    # -- scheduling -----------------------------------------------------
    def reserve(self) -> float:
        """Claim the next request slot and return how long the caller must wait."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next_slot, self._cooldown_until)
            spread = self._interval * self.jitter
            gap = max(0.05, self._interval + self._rng.uniform(-spread, spread))
            self._next_slot = start + gap
            return start - now

    def acquire(self, cancel: threading.Event | None = None) -> bool:
        """Block until this caller may send a request. Returns ``False`` if cancelled."""
        wait = self.reserve()
        if wait <= 0:
            return not (cancel and cancel.is_set())
        if cancel is None:
            time.sleep(wait)
            return True
        return not cancel.wait(wait)

    # -- feedback -------------------------------------------------------
    def record_success(self) -> None:
        with self._lock:
            self._consecutive_warnings = 0
            self._clean_streak += 1
            if self._clean_streak >= self.recover_after and self._interval > self.base_interval:
                self._interval = max(self.base_interval, self._interval * 0.8)
                self._clean_streak = 0

    def record_warning(self, retry_after: float | None = None) -> float:
        """Register a throttling signal. Returns the cool-down applied (seconds)."""
        with self._lock:
            now = self._clock()
            self._total_warnings += 1
            self._consecutive_warnings += 1
            self._clean_streak = 0
            self._interval = min(self.max_interval, max(self._interval * 2, self.base_interval * 2))
            if self._consecutive_warnings >= self.breaker_threshold:
                self._breaker_trips += 1
                self._consecutive_warnings = 0
                cooldown = self.breaker_cooldown
            else:
                cooldown = self.warning_cooldown * self._consecutive_warnings
            if retry_after is not None:
                cooldown = max(cooldown, min(retry_after, 900.0))
            cooldown *= self._rng.uniform(1.0, 1.25)
            self._cooldown_until = max(self._cooldown_until, now + cooldown)
            return cooldown

    def snapshot(self) -> LimiterSnapshot:
        with self._lock:
            return LimiterSnapshot(
                base_interval=self.base_interval,
                current_interval=self._interval,
                cooldown_remaining=max(0.0, self._cooldown_until - self._clock()),
                consecutive_warnings=self._consecutive_warnings,
                total_warnings=self._total_warnings,
                breaker_trips=self._breaker_trips,
            )
