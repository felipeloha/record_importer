import time
from collections.abc import Callable


class TokenBucket:
    """`rate` tokens per `per` seconds, burst up to `capacity`. Clock and sleep are injectable for tests."""

    def __init__(
        self,
        rate: float = 60,
        per: float = 60.0,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.fill_rate = rate / per
        self.capacity = capacity if capacity is not None else rate
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.last = clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.fill_rate)
        self.last = now

    def acquire(self) -> None:
        self._refill()
        if self.tokens < 1:
            self.sleep((1 - self.tokens) / self.fill_rate)
            self._refill()
        self.tokens -= 1
