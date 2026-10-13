"""Historical subscription ownership comes directly from indexed order columns."""

import pytest
from types import SimpleNamespace

import pm_nautilus.store as store_module
from pm_nautilus.store import Store


def test_historical_tokens_need_no_json_decode_and_follow_rollback_and_reset(tmp_path, monkeypatch):
    store = Store(tmp_path / "TEST.sqlite")
    intent = {"event_id": "event", "side": "BUY", "terminal": True, "token_id": "1"}
    try:
        store.save_intent("first", intent)
        store.save_intent("second", intent | {"token_id": "2", "terminal": False})
        store.save_intent("duplicate", intent)
        with monkeypatch.context() as context:
            context.setattr(
                store_module.json, "loads", lambda _: pytest.fail("JSON decode unnecessary")
            )
            assert store.historical_token_ids() == {"1", "2"}
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.save_intent("rollback", intent | {"token_id": "3"})
                raise RuntimeError("controlled rollback")
        assert store.historical_token_ids() == {"1", "2"}
        store.reset()
        assert store.historical_token_ids() == set()
    finally:
        store.close()


def test_native_event_prefix_bounds_sql_before_decoding_unrelated_ahead_facts(tmp_path):
    store = Store(tmp_path / "TEST.sqlite")
    decoded = []

    def deserialize(payload):
        decoded.append(payload)
        return payload

    store.serializer = SimpleNamespace(deserialize=deserialize)
    try:
        for index in range(1, 5):
            store.db.execute(
                "INSERT INTO native_events(event_id,kind,payload) VALUES(?,?,?)",
                (str(index), "test", b"{}"),
            )
        assert [seq for seq, _ in store.events(1, until=3)] == [2, 3]
        assert len(decoded) == 2
        decoded.clear()
        assert store.events(3, until=3) == [] and decoded == []
        assert [seq for seq, _ in store.events(2)] == [3, 4]
    finally:
        store.close()
