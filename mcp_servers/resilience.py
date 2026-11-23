"""Circuit breaker, retry with exponential backoff, TTL cache and a guarded caller with
fallback provider. Clock and sleep are injectable so behaviour is testable."""
import time
from collections.abc import Callable
from functools import partial
from typing import Any

_MISSING = object()


class SupplierUnavailable(Exception):
    pass


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        reset_after: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.reset_after:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == "half_open" or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()


class TTLCache:
    def __init__(self, ttl: float = 300.0, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any:
        item = self._data.get(key)
        if item is None or self._clock() - item[0] > self._ttl:
            return _MISSING
        return item[1]

    def set(self, key: Any, value: Any) -> None:
        self._data[key] = (self._clock(), value)


def call_with_retry(
    fn: Callable[[], Any],
    retries: int = 2,
    base_delay: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception:
            if attempt == retries:
                raise
            sleep(base_delay * (2**attempt))
    raise AssertionError("unreachable")


class Guarded:
    """Wraps a primary (and optional fallback) supplier with breaker + retry + cache."""

    def __init__(
        self,
        primary: Any,
        fallback: Any | None = None,
        *,
        retries: int = 2,
        base_delay: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        cache_ttl: float = 300.0,
    ):
        self.targets = [("primary", primary)] + ([("fallback", fallback)] if fallback else [])
        self.breakers = {name: CircuitBreaker(clock=clock) for name, _ in self.targets}
        self.cache = TTLCache(cache_ttl, clock)
        self._retries, self._base_delay, self._sleep = retries, base_delay, sleep

    def call(self, method: str, *args: Any, cacheable: bool = False) -> Any:
        key = (method, args)
        if cacheable:
            hit = self.cache.get(key)
            if hit is not _MISSING:
                return hit
        last: Exception | None = None
        for name, target in self.targets:
            breaker = self.breakers[name]
            if not breaker.allow():
                continue
            try:
                result = call_with_retry(
                    partial(getattr(target, method), *args),
                    self._retries,
                    self._base_delay,
                    self._sleep,
                )
            except Exception as e:  # noqa: BLE001 - any supplier error counts as a failure
                breaker.record_failure()
                last = e
                continue
            breaker.record_success()
            if cacheable:
                self.cache.set(key, result)
            return result
        raise SupplierUnavailable(f"{method}: all suppliers unavailable") from last
