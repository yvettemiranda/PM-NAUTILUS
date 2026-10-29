"""Empty TEST quote deferral observes local, foreign and rolled-back SQL work."""

import json
import sqlite3
from unittest.mock import Mock

import pytest

from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime
from pm_nautilus.test_runtime import TestRuntime

from test_test_runtime_efficiency import make_runtime, quote


@pytest.fixture
def runtime(tmp_path):
    current, _, _ = make_runtime(tmp_path)
    quote(current)
    assert current._empty_paused_without_work()
    try:
        yield current
    finally:
        current.close()


def active_intent(runtime, *, foreign=False):
    payload = {
        "event_id": "event",
        "token_id": "1",
        "side": "BUY",
        "cash": 1,
        "terminal": False,
    }
    values = ("pending", "event", "1", "BUY", json.dumps(payload))
    if foreign:
        with sqlite3.connect(runtime.store.path) as writer:
            writer.execute("INSERT INTO intents VALUES(?,?,?,?,?)", values)
    else:
        runtime.store.db.execute("INSERT INTO intents VALUES(?,?,?,?,?)", values)
    return payload


def test_stable_empty_quotes_use_one_read_only_version_check_and_preserve_pending_work(runtime):
    sql = []
    changes = runtime.store.db.total_changes
    dirty = set(runtime.dirty)
    preview = set(runtime._test_preview_pending)
    runtime.store.db.set_trace_callback(sql.append)
    try:
        runtime.drain()
    finally:
        runtime.store.db.set_trace_callback(None)
    assert sql == ["PRAGMA data_version"]
    assert runtime.dirty == dirty and runtime._test_preview_pending == preview
    assert runtime.store.db.total_changes == changes


@pytest.mark.parametrize("foreign", [False, True])
def test_raw_status_change_is_seen_on_the_next_drain_and_uses_real_start_rules(runtime, foreign):
    before = runtime._test_empty_paused_proof
    if foreign:
        with sqlite3.connect(runtime.store.path) as writer:
            writer.execute("UPDATE meta SET value='\"RUNNING\"' WHERE key='status'")
    else:
        runtime.store.db.execute("UPDATE meta SET value='\"RUNNING\"' WHERE key='status'")
    runtime.drain()
    assert runtime.status == "RUNNING"
    assert runtime.store.intents() and runtime.business["cycles"]
    assert not runtime._test_can_defer
    assert runtime._test_empty_paused_proof != before
    assert runtime.validate()["ok"]


@pytest.mark.parametrize("foreign", [False, True])
def test_raw_active_intent_change_cannot_hit_a_previous_empty_proof(runtime, foreign):
    active_intent(runtime, foreign=foreign)
    assert not runtime._empty_paused_without_work()
    assert runtime._test_empty_paused_proof is None


def test_reading_transactional_empty_state_never_hides_an_active_intent_after_rollback(runtime):
    payload = active_intent(runtime)
    assert not runtime._empty_paused_without_work()
    runtime.store.db.execute("BEGIN")
    payload["terminal"] = True
    runtime.store.db.execute("UPDATE intents SET payload=?", (json.dumps(payload),))
    assert runtime.store.intents(active_only=True) == {}
    assert not runtime._empty_paused_without_work()
    assert runtime._test_empty_paused_proof is None
    runtime.store.db.execute("ROLLBACK")
    assert runtime.store.intents(active_only=True)
    assert not runtime._empty_paused_without_work()


def test_even_a_read_only_transaction_cannot_reuse_a_previous_proof(runtime):
    runtime.store.db.execute("BEGIN")
    try:
        assert not runtime._empty_paused_without_work()
        assert runtime._test_empty_paused_proof is None
    finally:
        runtime.store.db.execute("ROLLBACK")
    assert runtime._empty_paused_without_work()


def test_external_commit_between_validation_reads_cannot_establish_a_proof(runtime, monkeypatch):
    runtime._test_empty_paused_proof = None
    original = runtime.store.intents

    def foreign_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        with sqlite3.connect(runtime.store.path) as writer:
            writer.execute("INSERT INTO meta VALUES('guard_probe','true')")
        return result

    monkeypatch.setattr(runtime.store, "intents", foreign_commit)
    assert not runtime._empty_paused_without_work()
    assert runtime._test_empty_paused_proof is None


@pytest.mark.parametrize("foreign", [False, True])
def test_generation_change_rebuilds_the_proof(runtime, foreign):
    before = runtime._test_empty_paused_proof
    sql = "UPDATE meta SET value='\"new-generation\"' WHERE key='generation'"
    if foreign:
        with sqlite3.connect(runtime.store.path) as writer:
            writer.execute(sql)
    else:
        runtime.store.db.execute(sql)
    assert runtime._empty_paused_without_work()
    assert runtime._test_empty_paused_proof != before
    assert runtime._test_empty_paused_proof[1] == "new-generation"


@pytest.mark.parametrize("collection", ["cycles", "targets", "claims"])
def test_each_in_memory_financial_collection_immediately_disables_the_empty_guard(
    runtime, collection
):
    runtime.business[collection]["owned"] = {"state": "CREDITED"}
    try:
        assert not runtime._empty_paused_without_work()
        assert runtime._test_empty_paused_proof is None
    finally:
        runtime.business[collection].clear()


def test_closed_recovery_and_reentrant_states_use_the_old_path(runtime):
    for field, value in (("recovery_error", "controlled"),):
        runtime.business[field] = value
        assert not runtime._empty_paused_without_work()
        runtime.business[field] = None
    runtime.store._closed = True
    try:
        assert not runtime._empty_paused_without_work()
    finally:
        runtime.store._closed = False
    runtime._paused_dirty_capture = set()
    try:
        assert not runtime._empty_paused_without_work()
    finally:
        runtime._paused_dirty_capture = None


def test_replacing_a_store_cannot_reuse_its_predecessors_proof(runtime, tmp_path):
    old = runtime.store
    replacement = Store(tmp_path / "replacement.sqlite")
    previous = runtime._test_empty_paused_proof
    runtime.store = replacement
    try:
        assert runtime._empty_paused_without_work()
        assert runtime._test_empty_paused_proof != previous
        assert runtime._test_empty_paused_proof[0][0] is replacement
    finally:
        runtime.store = old
        replacement.close()


def test_pause_start_owned_exit_and_reset_keep_the_original_rules(runtime, tmp_path):
    runtime.pause()
    assert runtime._test_empty_paused_proof is None
    runtime.start()
    runtime.pause()
    assert runtime.business["cycles"] and not runtime._empty_paused_without_work()
    quote(runtime, bid=90_000, ask=100_000)
    assert not runtime.business["cycles"] and runtime.validate()["ok"]
    runtime.store.reset()
    generation = runtime.store.generation
    path = runtime.store.path
    runtime.close()
    restored = TestRuntime(path, clock=runtime.native.clock)
    try:
        assert restored.status == "PAUSED" and restored.store.generation == generation
        assert restored._test_empty_paused_proof is None
        assert restored._empty_paused_without_work()
        assert not restored.store.intents() and not restored.store.events()
    finally:
        restored.close()
    # The fixture owns the original already-closed object.
    runtime.close = Mock()


def test_running_or_faulted_runtime_does_not_enter_empty_guard(runtime, monkeypatch):
    guard = Mock(side_effect=AssertionError("empty guard entered"))
    monkeypatch.setattr(runtime, "_empty_paused_without_work", guard)
    runtime._test_can_defer = False
    runtime.drain()
    runtime.faulted = "controlled"
    runtime._test_can_defer = True
    runtime.drain()
    guard.assert_not_called()
    assert Runtime.drain is not TestRuntime.drain
