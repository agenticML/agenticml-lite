"""The per-process Postgres connection budget."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import logging
import threading
import time

import pytest

from agenticml.server import app
from agenticml.server.db import DatabaseBusy, postgres
from agenticml.server.db.postgres import (
    DEFAULT_LIMITS, MIN_BUDGET, configure_connection_budget, connection_limits)


@pytest.fixture(autouse=True)
def restore_defaults():
    yield
    configure_connection_budget(None)


def _limits():
    return (postgres.TRANSACTION_CONNECTION_LIMIT, postgres._outer_slots._initial_value,
            postgres._inner_slots._initial_value)


def test_none_keeps_the_defaults():
    assert connection_limits(None) == DEFAULT_LIMITS == (24, 48, 16)
    assert MIN_BUDGET == 8
    assert postgres.SESSION_LOCK_LIMIT == 64


def test_default_budget_fits_the_defaults_plus_the_server_lock():
    assert sum(DEFAULT_LIMITS) + 1 == 89


@pytest.mark.parametrize("budget", range(8, 201))
def test_every_budget_is_spent_exactly(budget):
    transaction, outer, inner = connection_limits(budget)
    assert transaction + outer + inner + 1 == budget
    assert transaction >= 2 and outer >= 1 and inner >= 1


def test_hosted_budgets():
    assert [connection_limits(n) for n in (25, 16, 12)] == [(6, 14, 4), (4, 7, 4), (3, 4, 4)]


@pytest.mark.parametrize("budget", range(8, 201))
def test_inner_pool_keeps_room_beyond_two_lineage_receives(budget):
    assert connection_limits(budget)[2] >= 4


@pytest.mark.parametrize("budget", [7, 0, -1, 1001, 8.0, "25", True, False, [8]])
def test_invalid_budgets_are_refused(budget):
    with pytest.raises(ValueError, match="whole number from 8 to 1000"):
        connection_limits(budget)


def test_maximum_budget():
    transaction, outer, inner = connection_limits(1000)
    assert transaction + outer + inner + 1 == 1000


def test_configure_sets_and_restores_the_limits():
    configure_connection_budget(25)
    assert _limits() == connection_limits(25)
    configure_connection_budget(None)
    assert _limits() == DEFAULT_LIMITS
    configure_connection_budget(12)
    configure_connection_budget(12)
    assert _limits() == connection_limits(12)
    configure_connection_budget(None)
    assert _limits() == DEFAULT_LIMITS


def test_invalid_budget_leaves_the_limits_alone():
    configure_connection_budget(25)
    with pytest.raises(ValueError):
        configure_connection_budget(3)
    assert _limits() == connection_limits(25)


def test_old_semaphore_holder_releases_after_a_reconfigure():
    old = postgres._inner_slots
    assert old.acquire(timeout=1)
    configure_connection_budget(25)
    assert postgres._inner_slots is not old
    old.release()
    assert old._value == old._initial_value
    new = postgres._inner_slots
    assert new.acquire(blocking=False)
    new.release()


def test_small_budget_gives_a_small_transaction_limit():
    configure_connection_budget(8)
    assert postgres.TRANSACTION_CONNECTION_LIMIT == 2


def test_budget_on_sqlite_logs_and_changes_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("AGENTICML_SERVER_DATABASE_URL", raising=False)
    caplog.set_level(logging.INFO, logger=app.logger.name)
    configure_connection_budget(25)
    assert _limits() == connection_limits(25)
    with app.make_server(tmp_path / "data", port=0, db_connections=25):
        assert _limits() == DEFAULT_LIMITS
    assert caplog.text.count("database connection budget applies to Postgres only") == 1
    caplog.clear()
    configure_connection_budget(25)
    with app.make_server(tmp_path / "data", port=0):
        assert _limits() == DEFAULT_LIMITS
    assert "database connection budget" not in caplog.text


def test_serve_passes_db_connections_to_make_server(tmp_path, monkeypatch):
    seen = []

    class Stop(Exception):
        pass

    def recorder(*args, **kwargs):
        seen.append(kwargs)
        raise Stop

    monkeypatch.setattr(app, "make_server", recorder)
    with pytest.raises(Stop):
        app.serve(tmp_path / "data", port=0, db_connections=25, stats_interval=0)
    with pytest.raises(Stop):
        app.serve(tmp_path / "data", port=0, stats_interval=0)
    assert seen[0]["db_connections"] == 25
    assert "db_connections" not in seen[1]


def test_invalid_budget_stops_make_server(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICML_SERVER_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="whole number"):
        app.make_server(tmp_path / "data", port=0, db_connections=3)


@pytest.fixture
def pg_server(tmp_path, server_database):
    if server_database is None:
        pytest.skip("AGENTICML_TEST_DATABASE_URL is not set")
    servers = []

    def start(budget):
        server = app.make_server(tmp_path / "data", port=0, db_connections=budget)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.server_close()


def test_make_server_applies_the_budget_to_postgres(pg_server):
    server = pg_server(25)
    transaction, outer, inner = connection_limits(25)
    assert _limits() == (transaction, outer, inner)
    assert server.db._tx_slots._initial_value == transaction
    assert server.db._idle_cap == min(16, transaction)


def test_make_server_without_a_budget_restores_the_defaults(pg_server):
    pg_server(12).server_close()
    assert _limits() == connection_limits(12)
    server = pg_server(None)
    assert _limits() == DEFAULT_LIMITS
    assert server.db._tx_slots._initial_value == 24


def bounded(body, seconds=60):
    """Run body in a daemon thread and fail instead of hanging.

    A daemon thread, unlike a ThreadPoolExecutor worker, is not joined at
    interpreter exit, so a real hang cannot outlive the failed test.
    """
    outcome = {}

    def run():
        try:
            outcome["result"] = body()
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(f"timed out after {seconds} seconds")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def test_make_server_refuses_a_database_open_under_other_limits(pg_server):
    server = pg_server(25)
    with pytest.raises(ValueError, match="already open in this process with other connection limits"):
        pg_server(12)
    # The first owner keeps its instance and its limits.
    assert not server.db.closed
    assert _limits() == connection_limits(25)
    with server.db.transaction() as tx:
        assert tx.fetchone("SELECT 1")[0] == 1
    assert pg_server(25).db is server.db


def test_budgets_sharing_the_transaction_limit_are_still_refused(pg_server):
    # (6, 13, 4) and (6, 14, 4) differ only in the lock pools.
    assert connection_limits(24)[0] == connection_limits(25)[0]
    server = pg_server(24)
    outer, inner = postgres._outer_slots, postgres._inner_slots
    with pytest.raises(ValueError, match="already open in this process with other connection limits"):
        pg_server(25)
    assert server.db.limits == connection_limits(24)
    assert _limits() == connection_limits(24)
    assert postgres._outer_slots is outer and postgres._inner_slots is inner


def test_the_same_budget_twice_keeps_the_same_instance_and_semaphores(pg_server):
    first = pg_server(24)
    outer, inner = postgres._outer_slots, postgres._inner_slots
    second = pg_server(24)
    assert second.db is first.db
    assert postgres._outer_slots is outer and postgres._inner_slots is inner


def test_lineage_receives_leave_the_quota_locks_free(pg_server):
    server = pg_server(12)
    db = server.db
    assert server.lineage_receives._initial_value == 2
    with ExitStack() as locks:
        for index in range(2):
            locks.enter_context(db.session_lock(f"lineage:ws:project-{index}", timeout=5))
        started = time.monotonic()
        with db.session_lock("quota:ws", timeout=1):
            pass
        assert time.monotonic() - started < 1


def test_work_beyond_the_budget_times_out_instead_of_hanging(pg_server, monkeypatch):
    monkeypatch.setattr(postgres, "_TRANSACTION_WAIT", .2)

    def body():
        server = pg_server(8)
        db = server.db
        transaction, outer, inner = connection_limits(8)
        assert (transaction, outer, inner) == (2, 1, 4)
        release = threading.Event()
        entered = threading.Barrier(transaction + 1)

        def hold_transaction():
            with db.transaction():
                entered.wait(timeout=15)
                release.wait(timeout=15)

        with ThreadPoolExecutor(transaction) as pool:
            holders = [pool.submit(hold_transaction) for _ in range(transaction)]
            entered.wait(timeout=15)
            with pytest.raises(DatabaseBusy):
                with db.transaction():
                    pass
            assert db.pool_stats()["in_use"] <= 8
            release.set()
            for holder in holders:
                holder.result(timeout=15)
        with ExitStack() as locks:
            for index in range(outer):
                locks.enter_context(db.session_lock(f"run:{index}", timeout=5))
            assert db.pool_stats()["in_use"] <= 8
            with pytest.raises(DatabaseBusy):
                with db.session_lock("run:over", timeout=.2):
                    pass
            for index in range(inner):
                locks.enter_context(db.session_lock(f"quota:{index}", timeout=5))
            with pytest.raises(DatabaseBusy):
                with db.session_lock("quota:over", timeout=.2):
                    pass
            assert db.pool_stats()["peak"] <= 8
        assert db.pool_stats()["in_use"] <= 8
        with db.transaction() as tx:
            assert tx.fetchone("SELECT 1")[0] == 1

    bounded(body)


def test_pool_never_exceeds_the_budget_under_load(pg_server, postgres_admin):
    def body():
        server = pg_server(8)
        db = server.db
        db.pool_stats(reset=True)
        seen = []
        backends = []
        stop = threading.Event()

        def count_backends():
            # One admin connection, used only by this thread while the load runs.
            while not stop.is_set():
                backends.append(postgres_admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND usename = 'agenticml_test' AND pid <> pg_backend_pid()").fetchone()[0])
                time.sleep(.005)

        def work(index):
            try:
                with db.session_lock(f"run:{index % 6}", timeout=10):
                    with db.transaction() as tx:
                        tx.fetchone("SELECT 1")
                        with db._lock:
                            seen.append((len(db._connections), db._lock_connections))
            except DatabaseBusy:
                pass

        sampler = threading.Thread(target=count_backends)
        sampler.start()
        try:
            with ThreadPoolExecutor(16) as pool:
                list(pool.map(work, range(64)))
        finally:
            stop.set()
            sampler.join(timeout=15)
        assert seen and backends
        assert max(t for t, _ in seen) <= postgres.TRANSACTION_CONNECTION_LIMIT
        assert max(t + lock for t, lock in seen) <= 8
        assert max(backends) <= 8
        assert db.pool_stats()["peak"] <= 8

    bounded(body)
