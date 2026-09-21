"""Token-bucket rate limiter for the Riot API.

Development keys are limited to roughly 20 requests/second and 100 requests per
2 minutes. We enforce both windows client-side so ingestion does not get 429'd.
Production keys have higher limits — adjust via the constructor.
"""

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window limiter enforcing multiple (count, seconds) limits."""

    def __init__(self, limits=((20, 1), (100, 120))):
        # limits: iterable of (max_requests, per_seconds)
        self._limits = list(limits)
        self._hits = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request is permitted under all configured windows."""
        while True:
            with self._lock:
                now = time.monotonic()
                longest = max(s for _, s in self._limits)
                while self._hits and now - self._hits[0] > longest:
                    self._hits.popleft()

                wait = 0.0
                for max_req, per_s in self._limits:
                    recent = sum(1 for t in self._hits if now - t <= per_s)
                    if recent >= max_req:
                        oldest = next(t for t in self._hits if now - t <= per_s)
                        wait = max(wait, per_s - (now - oldest))

                if wait <= 0:
                    self._hits.append(now)
                    return
            time.sleep(wait + 0.01)
