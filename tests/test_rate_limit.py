from app.rate_limit import InMemoryRateLimiter, SQLiteRateLimiter
from app.storage import SQLiteStore


def _claim_from_process(db_path: str, queue) -> None:
    store = SQLiteStore(db_path)
    try:
        claim = store.claim_message("shared-message", "shared-customer", "hello", processing_timeout=120)
        queue.put(claim.state)
    finally:
        store.close()


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


def test_message_claim_is_unique_across_real_processes(tmp_path):
    """Attack the claim boundary with independent OS processes/connections."""
    import multiprocessing

    db_path = str(tmp_path / "claims.db")
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=_claim_from_process, args=(db_path, queue)) for _ in range(8)]
    for worker in workers:
        worker.start()
    states = [queue.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert states.count("claimed") == 1
    assert states.count("processing") == len(workers) - 1
