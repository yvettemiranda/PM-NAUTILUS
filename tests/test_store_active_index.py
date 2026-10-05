"""The global active-intent read uses a small partial index on growing history."""

from pm_nautilus.store import Store


def test_active_intent_index_avoids_historical_scan(tmp_path):
    store = Store(tmp_path / "orders.sqlite")
    try:
        with store.transaction():
            for number in range(2_000):
                store.save_intent(
                    f"closed-{number}",
                    {
                        "event_id": f"event-{number % 10}",
                        "token_id": "token",
                        "side": "BUY",
                        "terminal": True,
                    },
                )
            store.save_intent(
                "open",
                {"event_id": "event-0", "token_id": "token", "side": "BUY", "terminal": False},
            )

        plan = store.db.execute(
            "EXPLAIN QUERY PLAN SELECT order_id,payload FROM intents "
            "WHERE json_extract(payload,'$.terminal')=0"
        ).fetchall()
        assert any("intents_open" in row[3] for row in plan), plan
        assert list(store.intents(active_only=True)) == ["open"]
        assert list(store.intents(active_only=True, event_id="event-0")) == ["open"]
        assert store.intents(active_only=True, event_id="event-1") == {}
        assert set(store.intents(event_id="event-0")) == {
            "open",
            *(f"closed-{number}" for number in range(0, 2_000, 10)),
        }
        indexes = {row[1] for row in store.db.execute("PRAGMA index_list(intents)")}
        assert {"intents_open", "intents_active"} <= indexes
    finally:
        store.close()


def test_active_intent_index_is_added_to_existing_database(tmp_path):
    path = tmp_path / "existing.sqlite"
    store = Store(path)
    try:
        store.save_intent(
            "open",
            {"event_id": "event-0", "token_id": "token", "side": "BUY", "terminal": False},
        )
        # Simulate a database created by a version before the new partial index.
        store.db.execute("DROP INDEX intents_open")
    finally:
        store.close()

    reopened = Store(path)
    try:
        indexes = {row[1] for row in reopened.db.execute("PRAGMA index_list(intents)")}
        assert "intents_open" in indexes
        assert list(reopened.intents(active_only=True)) == ["open"]
    finally:
        reopened.close()
