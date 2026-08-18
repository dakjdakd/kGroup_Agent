from app.rate_limit import InMemoryRateLimiter


def test_sliding_window_boundary():
    limiter = InMemoryRateLimiter()
    assert limiter.allow("c", "a", now=100.0)
    assert not limiter.allow("c", "b", now=159.9)
    assert limiter.allow("c", "c", now=160.1)
