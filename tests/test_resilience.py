import pytest

from mcp_servers.resilience import (
    CircuitBreaker,
    Guarded,
    SupplierUnavailable,
    TTLCache,
    call_with_retry,
)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_breaker_opens_then_half_opens_then_closes():
    clock = Clock()
    b = CircuitBreaker(failure_threshold=2, reset_after=10, clock=clock)
    b.record_failure()
    assert b.state == "closed"
    b.record_failure()
    assert b.state == "open" and not b.allow()
    clock.t = 11
    assert b.state == "half_open" and b.allow()
    b.record_success()
    assert b.state == "closed"


def test_half_open_failure_reopens():
    clock = Clock()
    b = CircuitBreaker(failure_threshold=1, reset_after=10, clock=clock)
    b.record_failure()
    clock.t = 11
    b.record_failure()
    assert b.state == "open"


def test_retry_uses_exponential_backoff():
    sleeps, calls = [], {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError
        return "ok"

    assert call_with_retry(flaky, retries=2, base_delay=1, sleep=sleeps.append) == "ok"
    assert sleeps == [1, 2]


def test_retry_raises_after_exhaustion():
    def boom():
        raise ConnectionError

    with pytest.raises(ConnectionError):
        call_with_retry(boom, retries=1, sleep=lambda _: None)


def test_cache_ttl():
    clock = Clock()
    c = TTLCache(ttl=5, clock=clock)
    c.set("k", 1)
    assert c.get("k") == 1
    clock.t = 6
    assert c.get("k") != 1  # expired -> sentinel


class Supplier:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def get(self, x):
        self.calls += 1
        if self.fail:
            raise ConnectionError
        return f"v{x}"


def test_fallback_used_when_primary_down():
    g = Guarded(Supplier(fail=True), Supplier(), retries=0, sleep=lambda _: None)
    assert g.call("get", 1) == "v1"


def test_open_breaker_stops_hammering_primary():
    primary = Supplier(fail=True)
    g = Guarded(primary, Supplier(), retries=0, sleep=lambda _: None)
    for i in range(10):
        g.call("get", i)
    assert primary.calls == 3  # breaker opened after threshold


def test_all_down_raises():
    g = Guarded(Supplier(fail=True), retries=0, sleep=lambda _: None)
    with pytest.raises(SupplierUnavailable):
        g.call("get", 1)


def test_cacheable_calls_hit_supplier_once():
    s = Supplier()
    g = Guarded(s)
    g.call("get", 1, cacheable=True)
    g.call("get", 1, cacheable=True)
    assert s.calls == 1
