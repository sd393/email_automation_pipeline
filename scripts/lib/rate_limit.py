"""Token-bucket and sliding-window rate limiters.

Both classes accept ``clock`` and ``sleep`` callables so tests can drive them with
a synthetic monotonic clock (see ``FakeClock`` in ``tests/conftest.py``).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class RateLimiter:
    """Blocking token-bucket limiter.

    The bucket holds at most ``burst`` tokens and refills at ``rate_per_sec``.
    Each ``acquire()`` consumes one token; if none are available, the caller
    sleeps until the next token is ready.
    """

    def __init__(
        self,
        rate_per_sec: float,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rate = float(rate_per_sec)
        self.burst = int(burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        # Epsilon absorbs float drift: when we sleep exactly long enough for
        # one token, ``elapsed * rate`` can come back as 0.999...98 due to
        # rounding, leaving us in an infinite loop with vanishingly small
        # waits. Anything within 1e-9 of a full token counts as full.
        eps = 1e-9
        while True:
            with self._lock:
                now = self._clock()
                elapsed = now - self._last
                if elapsed > 0:
                    self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
                    self._last = now
                if self._tokens >= 1.0 - eps:
                    self._tokens = max(0.0, self._tokens - 1.0)
                    return
                needed = 1.0 - self._tokens
                wait = needed / self.rate
            self._sleep(wait)


class HourlyLimiter:
    """Sliding-window hourly cap.

    Keeps the timestamps of the most recent ``per_hour`` acquires in a deque.
    On ``acquire()``, prunes entries older than 3600s; if the deque has ``per_hour``
    entries, sleeps until the oldest ages out.

    The optional ``burst`` allows the first ``burst`` acquires to fire immediately
    when the limiter is empty (matters only at start-up; once the window fills, the
    hourly cap governs).
    """

    def __init__(
        self,
        per_hour: int,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_hour < 1:
            raise ValueError("per_hour must be >= 1")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.per_hour = int(per_hour)
        self.burst = int(burst)
        self._clock = clock
        self._sleep = sleep
        self._window: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                cutoff = now - 3600.0
                while self._window and self._window[0] <= cutoff:
                    self._window.popleft()
                if len(self._window) < self.per_hour:
                    self._window.append(now)
                    return
                wait = 3600.0 - (now - self._window[0])
                if wait < 0:
                    wait = 0.0
            self._sleep(wait)
