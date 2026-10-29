"""Normal TEST close releases owners after financial callbacks and ledger close."""

import gc
import sqlite3
import weakref
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nautilus_trader.common.component import TestClock

from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_construction import close_test_runtime, release_test_runtime
from pm_nautilus.test_runtime import create_runtime

from test_test_optimization_equivalence import DAY, token_set


def prepared_runtime(path, *, clock=None):
    if clock is None:
        clock = TestClock()
        clock.set_time(DAY)
    runtime = create_runtime(path, clock=clock)
    runtime.update_preferences({}, capital="3")
    tokens = token_set(4, fees=True)
    runtime.add_tokens(tokens)
    for token in tokens:
        runtime.book(token.token_id, [(40_000, 100_000_000)], [(50_000, 100_000_000)])
    runtime.start()
    return runtime, clock, tokens


def financial_rows(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            table: list(
                connection.execute(
                    f'SELECT * FROM "{table}"'
                    + (" WHERE key!='status'" if table == "meta" else "")
                    + " ORDER BY rowid"
                )
            )
            for table in tables
        }


def test_normal_close_preserves_financial_rows_and_original_callback_order(tmp_path):
    path = tmp_path / "state.sqlite"
    runtime, _, _ = prepared_runtime(path)
    before = financial_rows(path)
    calls = []
    pause, stop, close = runtime.pause, runtime.native.stop, runtime.store.close

    def call_pause():
        calls.append("pause")
        return pause()

    def call_stop():
        calls.append("stop")
        assert not runtime.store._closed
        return stop()

    def call_close():
        calls.append("store.close")
        assert not runtime.native.data.is_running
        assert not runtime.native.execution.is_running
        return close()

    runtime.pause, runtime.native.stop, runtime.store.close = call_pause, call_stop, call_close
    runtime.close()
    assert calls == ["pause", "stop", "store.close"]
    assert financial_rows(path) == before
    assert runtime.strategy.owner is None and runtime.client.owner is None
    assert runtime.native.owner is None
    assert not runtime.native.bus.subscriptions()
    assert not runtime.native.bus.endpoints()
    assert not runtime.native.cache.instruments()
    assert not runtime.native.cache.positions_open()
    runtime.close()
    release_test_runtime(runtime)
    assert calls == ["pause", "stop", "store.close"]


def test_real_native_and_owner_are_collected_after_repeated_close_reset(tmp_path):
    path = tmp_path / "state.sqlite"
    references = []

    def close_generation(number):
        runtime, _, _ = prepared_runtime(path)
        references.extend(
            (weakref.ref(runtime), weakref.ref(runtime.native), weakref.ref(runtime.store))
        )
        runtime.close()
        runtime.close()
        if number < 11:
            store = Store(path)
            try:
                store.reset()
            finally:
                store.close()

    for number in range(12):
        close_generation(number)
    gc.collect()
    assert all(reference() is None for reference in references)


class FailingResource:
    def __init__(self, resource, operation):
        self.resource = resource
        self.operation = operation

    def __getattr__(self, name):
        if name == self.operation:
            return Mock(side_effect=RuntimeError("controlled TEST cleanup failure"))
        return getattr(self.resource, name)


@pytest.mark.parametrize(
    ("resource", "operation"),
    [
        ("bus", "unsubscribe"),
        ("bus", "dispose"),
        ("portfolio", "dispose"),
        ("cache", "reset"),
        ("cache", "dispose"),
    ],
)
def test_one_cleanup_failure_does_not_retain_native_owner_or_open_database(
    tmp_path, resource, operation
):
    connection = None

    def close():
        nonlocal connection
        runtime, _, _ = prepared_runtime(tmp_path / "state.sqlite")
        connection = runtime.store.db
        references = weakref.ref(runtime), weakref.ref(runtime.native)
        native = runtime.native
        setattr(native, resource, FailingResource(getattr(native, resource), operation))
        runtime.close()
        assert native.owner is None
        assert runtime.strategy.owner is None and runtime.client.owner is None
        return references

    references = close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    gc.collect()
    assert all(reference() is None for reference in references)


def test_stop_failure_still_stops_other_local_engines_closes_store_and_releases_owner(tmp_path):
    connection = None

    def close():
        nonlocal connection
        runtime, _, _ = prepared_runtime(tmp_path / "state.sqlite")
        connection = runtime.store.db
        references = weakref.ref(runtime), weakref.ref(runtime.native)
        runtime.native.stop = Mock(side_effect=RuntimeError("controlled Native.stop failure"))
        try:
            runtime.close()
        except RuntimeError as exc:
            assert str(exc) == "controlled Native.stop failure"
        else:
            pytest.fail("original close error was hidden")
        assert runtime.native.data.is_stopped and runtime.native.execution.is_stopped
        runtime.close()
        return references

    references = close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    gc.collect()
    assert all(reference() is None for reference in references)


def test_independent_sibling_clock_timer_and_position_maintenance_survive_close(tmp_path):
    closing, _, _ = prepared_runtime(tmp_path / "closing.sqlite")
    healthy, clock, tokens = prepared_runtime(tmp_path / "healthy.sqlite")
    fired = []
    clock.set_time_alert_ns(
        "healthy_timer", DAY + 1_000_000_000, lambda event: fired.append(event.name)
    )
    try:
        healthy.pause()
        closing.close()
        assert clock.timer_names == ["healthy_timer"]
        assert healthy.native.data.is_running and healthy.native.execution.is_running
        assert healthy.native.bus.subscriptions()
        for event in clock.advance_time(DAY + 2_000_000_000):
            event.handle()
        assert fired == ["healthy_timer"]
        owned = healthy.tokens[healthy.business["cycles"][tokens[0].event_id]["token_id"]]
        healthy.book(owned.token_id, [(90_000, 100_000_000)], [(100_000, 100_000_000)])
        assert healthy.validate()["ok"]
        assert tokens[0].event_id not in healthy.business["cycles"]
    finally:
        closing.close()
        healthy.close()


def test_production_default_clocks_are_independent_and_sibling_timer_survives(tmp_path):
    closing = create_runtime(tmp_path / "closing.sqlite")
    healthy = create_runtime(tmp_path / "healthy.sqlite")
    clock = healthy.native.clock
    clock.set_time_alert_ns("healthy_timer", clock.timestamp_ns() + 60_000_000_000, lambda _: None)
    try:
        assert closing.native.clock is not clock
        closing.close()
        assert clock.timer_names == ["healthy_timer"]
        assert healthy.native.data.is_running and healthy.native.execution.is_running
    finally:
        clock.cancel_timer("healthy_timer")
        closing.close()
        healthy.close()


def test_cleanup_adds_no_shared_clock_callback_cancellation_beyond_original_stop(tmp_path):
    class ObservedClock(TestClock):
        def __init__(self):
            super().__init__()
            self.cancellations = []

        def cancel_timers(self):
            self.cancellations.append("timers")
            return super().cancel_timers()

        def cancel_callbacks(self):
            self.cancellations.append("callbacks")
            return super().cancel_callbacks()

        def cancel_default_handler(self):
            self.cancellations.append("default")
            return super().cancel_default_handler()

    observed = []
    for label, factory in (("original", Runtime), ("TEST", create_runtime)):
        clock = ObservedClock()
        clock.set_time(DAY)
        runtime = factory(tmp_path / f"{label}.sqlite", clock=clock)
        clock.set_time_alert_ns("injected_timer", DAY + 1_000_000_000, lambda _: None)
        runtime.close()
        observed.append((list(clock.cancellations), list(clock.timer_names)))
    # Existing SDK Actor.stop cancels the injected clock's timers. TEST cleanup
    # adds no all-clock callback/default-handler disposal on top of that limit.
    assert observed[0] == observed[1] == (["timers"], [])


def test_test_close_helpers_refuse_live_before_any_lifecycle_operation():
    live = SimpleNamespace(mode="LIVE", pause=Mock(), native=Mock(), store=Mock())
    for helper in (close_test_runtime, release_test_runtime):
        with pytest.raises(ValueError, match="不接受LIVE"):
            helper(live)
    live.pause.assert_not_called()
    assert not live.native.mock_calls and not live.store.mock_calls
