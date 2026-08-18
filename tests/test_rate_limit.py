from app.rate_limit import InMemoryRateLimiter, SQLiteRateLimiter
from app.storage import SQLiteStore


def test_sliding_window_boundary():
    limiter = InMemoryRateLimiter()
    assert limiter.allow("c", "a", now=100.0)
    assert not limiter.allow("c", "b", now=159.9)
    assert limiter.allow("c", "c", now=160.1)


def test_zero_timestamp_is_not_replaced_by_wall_clock():
    limiter = InMemoryRateLimiter()
    assert limiter.allow("c", "a", now=0.0)
    assert not limiter.allow("c", "b", now=59.0)


def test_sqlite_limiter_is_atomic_for_competing_threads():
    store = SQLiteStore(":memory:")
    limiter = SQLiteRateLimiter(store)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: limiter.allow("c", str(i), now=100.0), range(8)))
    assert sum(results) == 1


def test_session_creation_is_safe_across_independent_connections(tmp_path):
    db_path = str(tmp_path / "shared.db")
    stores = [SQLiteStore(db_path), SQLiteStore(db_path)]
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(lambda store: store.get_session("new-customer"), stores))
    assert [session.customer_id for session in sessions] == ["new-customer", "new-customer"]
    for store in stores:
        store.close()
