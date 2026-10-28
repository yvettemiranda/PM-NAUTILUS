"""Failed TEST construction releases resources before any account is published."""

import gc
import sqlite3
import weakref
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import pm_nautilus.native as native_module
import pm_nautilus.strategy as strategy_module
import pm_nautilus.store as store_module
from pm_nautilus.native import Native
from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_construction import construct_test_runtime
from pm_nautilus.test_runtime import TestRuntime, create_runtime

from test_runtime import setup


class ConstructionFailure(RuntimeError):
    pass


def capture_connections(monkeypatch, failure=None):
    connections = []
    connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if failure == "pragma" and sql == "PRAGMA journal_mode=WAL":
                raise ConstructionFailure("controlled PRAGMA failure")
            return super().execute(sql, *args)

        def executescript(self, sql, *args):
            result = super().executescript(sql, *args)
            if failure == "schema":
                raise ConstructionFailure("controlled schema failure")
            return result

    def tracked(*args, **kwargs):
        kwargs["factory"] = TrackedConnection
        result = connect(*args, **kwargs)
        connections.append(result)
        return result

    monkeypatch.setattr(store_module.sqlite3, "connect", tracked)
    return connections


def assert_connections_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("mode", ["TEST", "LIVE"])
@pytest.mark.parametrize("failure", ["pragma", "schema", "index", "final_status"])
def test_store_closes_opened_connection_on_every_initialization_failure(
    tmp_path, monkeypatch, mode, failure
):
    connections = capture_connections(monkeypatch, failure)
    if failure == "index":
        monkeypatch.setattr(
            Store, "ensure_token_index", Mock(side_effect=ConstructionFailure("index"))
        )
    elif failure == "final_status":
        original = Store._put_meta

        def fail_status(self, key, value):
            if key == "status":
                raise ConstructionFailure("final status")
            return original(self, key, value)

        monkeypatch.setattr(Store, "_put_meta", fail_status)
    with pytest.raises(ConstructionFailure):
        Store(tmp_path / "state.sqlite", mode)
    assert_connections_closed(connections)


def test_store_mode_refusal_closes_connection_without_mutating_existing_live_data(
    tmp_path, monkeypatch
):
    path = tmp_path / "LIVE.sqlite"
    original = Store(path, "LIVE")
    original.put("status", "RUNNING")
    expected = list(original.db.execute("SELECT * FROM meta ORDER BY key"))
    original.close()
    connections = capture_connections(monkeypatch)
    with pytest.raises(ValueError, match="其他模式"):
        Store(path, "TEST")
    assert_connections_closed(connections)
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as current:
        current.row_factory = sqlite3.Row
        assert list(current.execute("SELECT * FROM meta ORDER BY key")) == expected


@pytest.mark.parametrize("failure", ["after_init", "replay"])
def test_late_test_constructor_failure_disposes_native_closes_store_and_collects_owner(
    tmp_path, failure
):
    captured = {}

    def record(runtime):
        captured["runtime"] = weakref.ref(runtime)
        captured["native"] = weakref.ref(runtime.native)
        captured["store"] = weakref.ref(runtime.store)
        captured["connection"] = runtime.store.db
        captured["components"] = [
            runtime.native.strategy,
            runtime.native.execution,
            runtime.native.risk,
            runtime.native.data,
        ]

    class Broken(TestRuntime):
        def __init__(self, *args):
            super().__init__(*args)
            if failure == "after_init":
                record(self)
                raise ConstructionFailure("late TEST initialization failure")

        def recover_projection(self):
            if failure == "replay":
                record(self)
                raise ConstructionFailure("TEST replay failure")
            return super().recover_projection()

    def construct():
        try:
            construct_test_runtime(Broken, tmp_path / "state.sqlite")
        except ConstructionFailure:
            return
        pytest.fail("broken constructor returned an account")

    construct()
    assert all(component.is_disposed for component in captured["components"])
    assert_connections_closed([captured["connection"]])
    gc.collect()
    assert captured["runtime"]() is None
    assert captured["native"]() is None
    assert captured["store"]() is None


def test_native_rhs_failure_is_cleaned_before_runtime_native_was_assigned(tmp_path, monkeypatch):
    captured = {}
    native_init = Native.__init__

    def tracked_native(self, owner, clock=None):
        captured["native"] = weakref.ref(self)
        captured["runtime"] = weakref.ref(owner)
        captured["connection"] = owner.store.db
        return native_init(self, owner, clock)

    def fail_risk(*args, **kwargs):
        raise ConstructionFailure("controlled partial Native construction failure")

    monkeypatch.setattr(Native, "__init__", tracked_native)
    monkeypatch.setattr(native_module, "RiskEngine", fail_risk)

    def construct():
        try:
            construct_test_runtime(TestRuntime, tmp_path / "state.sqlite")
        except ConstructionFailure:
            return
        pytest.fail("partial Native failure returned an account")

    construct()
    assert_connections_closed([captured["connection"]])
    gc.collect()
    assert captured["native"]() is None
    assert captured["runtime"]() is None


def test_component_cleanup_failure_does_not_leave_other_resources_or_store_open(tmp_path):
    stopped, disposed, closed = [], [], []

    def component(name, failing=False):
        def stop():
            stopped.append(name)
            if failing:
                raise ConstructionFailure("controlled stop failure")

        return SimpleNamespace(is_running=True, stop=stop, dispose=lambda: disposed.append(name))

    class Broken(Runtime):
        def __init__(self, *_):
            self.mode = "TEST"
            self.native = object.__new__(Native)
            self.native.owner = self
            self.native.data = component("data")
            self.native.risk = component("risk")
            self.native.execution = component("execution", failing=True)
            self.native.strategy = component("strategy")
            self.native.bus = SimpleNamespace(dispose=lambda: disposed.append("bus"))
            self.native.cache = SimpleNamespace(dispose=lambda: disposed.append("cache"))
            self.store = SimpleNamespace(close=lambda: closed.append(True))
            raise ConstructionFailure("original construction failure")

    with pytest.raises(ConstructionFailure, match="original construction failure"):
        construct_test_runtime(Broken, tmp_path / "unused.sqlite")
    assert set(stopped) == {"strategy", "execution", "risk", "data"}
    assert set(disposed) == {"strategy", "execution", "risk", "data", "bus", "cache"}
    assert closed == [True]


def test_native_factory_failure_after_construction_also_releases_its_unassigned_result(
    tmp_path, monkeypatch
):
    captured = {}

    def broken_factory(owner, clock):
        native = Native(owner, clock)
        captured["runtime"] = weakref.ref(owner)
        captured["native"] = weakref.ref(native)
        captured["connection"] = owner.store.db
        raise ConstructionFailure("controlled wrapper failure before assignment")

    monkeypatch.setattr(strategy_module, "Native", broken_factory)

    def construct():
        try:
            construct_test_runtime(TestRuntime, tmp_path / "state.sqlite")
        except ConstructionFailure:
            return
        pytest.fail("failed native factory returned an account")

    construct()
    assert_connections_closed([captured["connection"]])
    gc.collect()
    assert captured["runtime"]() is None and captured["native"]() is None


def test_failed_test_disposal_does_not_stop_another_native_bus_or_ledger(tmp_path):
    reference, clock, token = setup(tmp_path / "reference")
    reference.close()
    healthy = create_runtime(tmp_path / "healthy.sqlite", clock=clock)
    try:
        healthy.add_tokens([token])
        healthy.book(token.token_id, [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        healthy.start()
        healthy.pause()
        assert healthy.cash() == 99_000_000

        class Broken(TestRuntime):
            def __init__(self, *args):
                super().__init__(*args)
                raise ConstructionFailure("late sibling initialization failure")

        with pytest.raises(ConstructionFailure):
            construct_test_runtime(Broken, tmp_path / "broken.sqlite")
        assert healthy.native.data.is_running and healthy.native.execution.is_running
        assert healthy.native.bus.subscriptions()
        assert not healthy.store._closed
        healthy.book(token.token_id, [(90_000, 100_000_000)], [(100_000, 50_000_000)])
        assert healthy.cash() == 103_500_000
        assert not healthy.business["cycles"] and healthy.validate()["ok"]
    finally:
        healthy.close()


def test_live_factory_never_enters_test_constructor_or_cleanup(tmp_path, monkeypatch):
    live = object()
    original = Mock(return_value=live)
    cleanup = Mock(side_effect=AssertionError("TEST helper entered LIVE construction"))
    monkeypatch.setattr("pm_nautilus.test_runtime.Runtime", original)
    monkeypatch.setattr("pm_nautilus.test_runtime.construct_test_runtime", cleanup)
    assert create_runtime(tmp_path / "LIVE.sqlite", mode="LIVE") is live
    original.assert_called_once()
    cleanup.assert_not_called()
    with pytest.raises(ValueError, match="不接受LIVE"):
        construct_test_runtime(Runtime, tmp_path / "must-not-exist.sqlite", mode="LIVE")
    assert not (tmp_path / "must-not-exist.sqlite").exists()
