"""Business row migration and atomic projection preserve canonical recovery."""

import json
import sqlite3
from copy import deepcopy

import pytest

from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime
from test_runtime import setup


def legacy_business():
    business = Store.empty_business()
    business.update(cursor=12, realized=25, settled=["condition"])
    business["cycles"]["event"] = {"token_id": "1", "quantity": 20}
    business["targets"]["target"] = {"event_id": "event", "qty": 20}
    business["claims"]["condition"] = {"state": "CREDITED", "amount": 25}
    return business


def write_legacy(path, business):
    store = Store(path)
    generation = store.generation
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE business_parts")
        db.execute("UPDATE meta SET value=? WHERE key='business'", (json.dumps(business),))
    return generation


def test_legacy_migration_reopen_and_atomic_rollback(tmp_path):
    path = tmp_path / "legacy.sqlite"
    original = legacy_business()
    generation = write_legacy(path, original)
    store = Store(path)
    assert store.generation == generation
    assert store.get("business") == original
    raw = json.loads(store.db.execute("SELECT value FROM meta WHERE key='business'").fetchone()[0])
    assert raw["_storage_version"] == 2 and raw["claims"] == {}
    assert store.db.execute("SELECT count(*) FROM business_parts").fetchone()[0] == 3
    changed = deepcopy(original)
    changed["cycles"]["event"]["quantity"] = 10
    changed["targets"].clear()
    changed["claims"]["new"] = {"state": "PENDING", "amount": 10}
    changed["cursor"] = 13
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put("business", changed)
            raise RuntimeError("after all business row writes")
    assert store.get("business") == original
    store.put("business", changed)
    store.close()
    store = Store(path)
    assert store.get("business") == changed and store.generation == generation
    store.close()


def test_failed_migration_keeps_the_complete_legacy_value(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    original = legacy_business()
    write_legacy(path, original)
    sync = Store._sync_business_rows

    def fail(self, collection, *args, **kwargs):
        sync(self, collection, *args, **kwargs)
        if collection == "claims":
            raise RuntimeError("migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_sync_business_rows", fail)
        with pytest.raises(RuntimeError):
            Store(path)
    with sqlite3.connect(path) as db:
        assert Store.read_business(db) == original
        assert db.execute("SELECT count(*) FROM business_parts").fetchone()[0] == 0
    store = Store(path)
    assert store.get("business") == original
    store.close()


@pytest.mark.parametrize("collection", ["cycles", "targets", "claims"])
@pytest.mark.parametrize("entry", ["unrelated", "identical", "conflicting"])
def test_v2_inline_rows_are_rejected_without_discarding_ledger(tmp_path, collection, entry):
    path = tmp_path / "state.sqlite"
    store = Store(path)
    business = legacy_business()
    store.put("business", business)
    raw = json.loads(store.db.execute("SELECT value FROM meta WHERE key='business'").fetchone()[0])
    key, value = next(iter(business[collection].items()))
    if entry == "unrelated":
        key = "unrelated"
    value = deepcopy(value)
    if entry == "conflicting":
        value["unexpected"] = True
    raw[collection][key] = value
    store.db.execute("UPDATE meta SET value=? WHERE key='business'", (json.dumps(raw),))
    store.put("status", "RUNNING")
    before = list(store.db.iterdump())
    with pytest.raises(ValueError, match="内联"):
        store.get("business")
    assert list(store.db.iterdump()) == before
    store.close()
    with pytest.raises(ValueError, match="内联"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


def test_unknown_storage_version_is_rejected_without_modifying_ledger(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path)
    store.put("business", legacy_business())
    raw = json.loads(store.db.execute("SELECT value FROM meta WHERE key='business'").fetchone()[0])
    raw["_storage_version"] = 3
    store.db.execute("UPDATE meta SET value=? WHERE key='business'", (json.dumps(raw),))
    store.put("status", "RUNNING")
    before = list(store.db.iterdump())
    store.close()
    with pytest.raises(ValueError, match="版本"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


def test_lost_storage_marker_cannot_discard_business_rows(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path)
    store.put("business", legacy_business())
    raw = Store.empty_business()
    store.db.execute("UPDATE meta SET value=? WHERE key='business'", (json.dumps(raw),))
    with pytest.raises(ValueError, match="冲突"):
        store.get("business")
    store.close()
    with pytest.raises(ValueError, match="冲突"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM business_parts").fetchone()[0] == 3
        assert (
            json.loads(db.execute("SELECT value FROM meta WHERE key='business'").fetchone()[0])
            == raw
        )


def test_event_save_does_not_serialize_unrelated_history(tmp_path, monkeypatch):
    import pm_nautilus.store as storage

    store = Store(tmp_path / "state.sqlite")
    business = legacy_business()
    business["claims"].update(
        {str(n): {"state": "CREDITED", "history_marker": n} for n in range(1000)}
    )
    store.put("business", business)
    dump = storage.json.dumps
    serialized = []

    def observed(value, *args, **kwargs):
        serialized.append(value)
        return dump(value, *args, **kwargs)

    business["cursor"] += 1
    business["cycles"]["event"]["quantity"] -= 1
    with monkeypatch.context() as patch:
        patch.setattr(storage.json, "dumps", observed)
        store.save_business_event(business, "event")
    assert not any(isinstance(value, dict) and "history_marker" in value for value in serialized)
    assert len(serialized) == 2  # Small scalar row plus the changed cycle.
    assert store.get("business") == business
    store.close()


def test_projection_failure_restores_only_touched_rows_and_replays(tmp_path, monkeypatch):
    runtime, clock, _ = setup(tmp_path)
    runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    runtime.start()
    runtime.pause()
    unrelated = {"state": "CREDITED", "amount": 25}
    runtime.business["claims"]["unrelated"] = unrelated
    runtime.store.put("business", runtime.business)
    before = {}
    sync = runtime.store._sync_business_rows
    project = runtime._project_transaction

    def observed_project(seq, event, intent, token):
        if type(event).__name__ == "OrderFilled":
            before.update(deepcopy(runtime.business))
        project(seq, event, intent, token)

    def fail_after_target_write(collection, values, *args, **kwargs):
        sync(collection, values, *args, **kwargs)
        if collection == "targets" and any(value["qty"] == 40_000_000 for value in values.values()):
            raise sqlite3.OperationalError("projection interruption")

    monkeypatch.setattr(runtime.store, "_sync_business_rows", fail_after_target_write)
    monkeypatch.setattr(runtime, "_project_transaction", observed_project)
    with pytest.raises(sqlite3.OperationalError):
        runtime.book("1", [(35_000, 10_000_000)], [(20_000, 50_000_000)])
    assert runtime.business == before
    assert runtime.business["claims"]["unrelated"] is unrelated
    assert runtime.store.get("business") == before
    assert runtime.faulted and runtime.status == "PAUSED"
    runtime.close()
    restored = Runtime(tmp_path / "test.sqlite", clock=clock)
    assert restored.business["cycles"]["event"]["quantity"] == 40_000_000
    assert restored.business["claims"]["unrelated"] == unrelated
    assert restored.validate()["ok"]
    restored.close()


def test_duplicate_native_delivery_does_not_advance_sequence_or_write_wal(tmp_path):
    runtime, _, _ = setup(tmp_path)
    runtime.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    runtime.start()
    runtime.pause()
    store = runtime.store
    events = store.events()
    before = store.db.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='native_events'"
    ).fetchone()[0]
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for seq, event in events:
        for _ in range(20):
            assert store.journal(event) == seq
    assert (
        store.db.execute("SELECT seq FROM sqlite_sequence WHERE name='native_events'").fetchone()[0]
        == before
    )
    assert store.path.with_name(store.path.name + "-wal").stat().st_size == 0
    assert store.events() == events
    runtime.close()
