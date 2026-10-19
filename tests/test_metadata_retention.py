"""Public cache retention cannot erase financial ownership or stall on history."""

from dataclasses import asdict, replace
import hashlib
import json
import sqlite3
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pm_nautilus.metadata_retention import prune_cold_metadata
from pm_nautilus.native import instrument_id
from pm_nautilus.store import Store
from test_token_storage import token

DAY = 86_400_000_000_000


def runtime_for(store):
    return SimpleNamespace(
        store=store,
        business=store.get("business"),
        tokens={},
        monitored=set(),
        books={},
        instrument_tokens={},
        native=SimpleNamespace(cache=SimpleNamespace(instrument=Mock(return_value=None))),
        faulted=None,
    )


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.db.execute("CREATE INDEX IF NOT EXISTS intents_token ON intents(token_id)")
    try:
        yield runtime_for(store)
    finally:
        store.db.close()


def cold(runtime, token_id, **changes):
    defaults = {
        "open": False,
        "event_id": f"event-{token_id}",
        "condition_id": f"condition-{token_id}",
        "ends_ns": DAY,
    }
    item = token(token_id, **(defaults | changes))
    runtime.store.save_token(item)
    return item


def prune(runtime, **kwargs):
    return prune_cold_metadata(runtime, 10 * DAY, time_budget=0.1, **kwargs)


def financial_fingerprint(db):
    result = {}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        if name != "tokens":
            rows = db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            result[name] = hashlib.sha256(repr([tuple(row) for row in rows]).encode()).hexdigest()
    return result


def test_only_cold_public_rows_are_deleted_and_financial_data_stays_exact(runtime):
    old = cold(runtime, "old")
    cold(runtime, "recent", ends_ns=11 * DAY)
    cold(runtime, "unknown-end", ends_ns=0)
    cold(runtime, "open", open=True)
    cold(runtime, "history")
    runtime.store.save_intent(
        "closed-order",
        {"token_id": "history", "event_id": "event-history", "side": "BUY", "terminal": True},
    )
    runtime.store.db.execute(
        "INSERT INTO native_events(event_id,kind,payload) VALUES(?,?,?)",
        ("durable-event", "history-fixture", b'{"client_order_id":"closed-order"}'),
    )
    runtime.tokens["old"] = old
    before = financial_fingerprint(runtime.store.db)

    result = prune(runtime)

    assert result["removed_ids"] == ("old",)
    assert result["removed"] == 1 and result["complete"] and result["skipped"] is None
    assert set(runtime.store.tokens()) == {"recent", "unknown-end", "open", "history"}
    assert financial_fingerprint(runtime.store.db) == before
    # The caller performs safe TokenRegistry eviction only after successful commit.
    assert runtime.tokens["old"] == old


def test_every_ownership_source_and_related_event_condition_is_preserved(runtime):
    names = (
        "intent-active",
        "intent-closed",
        "book",
        "memorybook",
        "cycle",
        "target",
        "claim",
        "rights",
        "native",
        "mapped",
        "monitored",
        "event-sibling",
        "condition-sibling",
        "free",
    )
    items = {name: cold(runtime, name) for name in names}
    for name, terminal in (("intent-active", False), ("intent-closed", True)):
        runtime.store.save_intent(
            name,
            {"token_id": name, "event_id": f"event-{name}", "side": "BUY", "terminal": terminal},
        )
    runtime.store.db.execute("INSERT INTO books VALUES(?,?)", ("book", "{}"))
    runtime.books["memorybook"] = object()
    runtime.business["cycles"]["event-cycle"] = {"token_id": "cycle"}
    runtime.business["targets"]["target-row"] = {"token_id": "target", "event_id": "event-target"}
    runtime.business["claims"]["condition-claim"] = {
        "token_id": "claim",
        "event_id": "event-claim",
        "condition_id": "condition-claim",
        "rights": {"rights": 0},
        "payouts": {"rights": 0},
        "state": "CREDITED",
    }
    runtime.store.save_token(replace(items["event-sibling"], event_id="event-cycle"))
    runtime.store.save_token(replace(items["condition-sibling"], condition_id="condition-claim"))
    runtime.monitored.add("monitored")
    runtime.instrument_tokens["native-mapped-id"] = "mapped"
    native_id = instrument_id(items["native"])
    runtime.native.cache.instrument.side_effect = lambda iid: object() if iid == native_id else None
    before = financial_fingerprint(runtime.store.db)

    result = prune(runtime)

    assert result["removed_ids"] == ("free",)
    assert set(runtime.store.tokens()) == set(names) - {"free"}
    assert financial_fingerprint(runtime.store.db) == before


def test_newer_memory_metadata_is_never_evicted(runtime):
    item = cold(runtime, "candidate")
    runtime.tokens[item.token_id] = replace(item, open=True)
    assert prune(runtime)["removed"] == 0
    assert "candidate" in runtime.store.tokens()


def test_legacy_flat_json_is_eligible_with_same_ownership_rules(runtime):
    item = cold(runtime, "legacy")
    runtime.store.db.execute(
        "UPDATE tokens SET payload=? WHERE token_id=?", (json.dumps(asdict(item)), item.token_id)
    )
    assert prune(runtime)["removed_ids"] == ("legacy",)


def test_keyset_pages_are_bounded_and_reset_only_after_complete_pass(runtime):
    for number in range(5):
        cold(runtime, f"{number:03}")
    first = prune(runtime, limit=2)
    assert first["scanned"] == first["removed"] == 2
    assert first["next_token_id"] == "001" and not first["complete"]
    second = prune(runtime, limit=2, after_token_id=first["next_token_id"])
    assert second["removed_ids"] == ("002", "003")
    last = prune(runtime, limit=2, after_token_id=second["next_token_id"])
    assert last["removed_ids"] == ("004",)
    assert last["complete"] and last["next_token_id"] is None
    assert prune(runtime)["complete"]


@pytest.mark.parametrize("malformed", ["metadata", "identity", "cycle", "claim", "unknown"])
def test_unknown_or_malformed_metadata_and_references_fail_closed(runtime, malformed):
    cold(runtime, "first")
    cold(runtime, "second")
    if malformed == "metadata":
        runtime.store.db.execute("UPDATE tokens SET payload='{}' WHERE token_id='second'")
    elif malformed == "identity":
        wrong = cold(runtime, "other")
        runtime.store.db.execute(
            "UPDATE tokens SET payload=? WHERE token_id='second'",
            (Store.encode_token_payload(asdict(wrong)),),
        )
    elif malformed == "cycle":
        runtime.business["cycles"]["event"] = {"token_id": 123}
    elif malformed == "claim":
        runtime.business["claims"]["condition"] = {
            "token_id": "foreign",
            "event_id": "event",
            "condition_id": "condition",
            "rights": {"second": "unknown"},
        }
    else:
        runtime.business["new_reference_collection"] = {"token_id": "second"}
    before = [
        tuple(row) for row in runtime.store.db.execute("SELECT * FROM tokens ORDER BY token_id")
    ]

    result = prune(runtime)

    assert result["removed"] == 0 and result["skipped"]
    assert [
        tuple(row) for row in runtime.store.db.execute("SELECT * FROM tokens ORDER BY token_id")
    ] == before
    assert not runtime.store.db.in_transaction


def test_missing_or_partial_ownership_index_refuses_unbounded_history_scan(runtime):
    cold(runtime, "candidate")
    runtime.store.db.execute("DROP INDEX intents_token")
    assert prune(runtime)["skipped"] == "missing_intent_index"
    runtime.store.db.execute("CREATE INDEX intents_token ON intents(token_id) WHERE side='BUY'")
    assert prune(runtime)["skipped"] == "missing_intent_index"
    assert "candidate" in runtime.store.tokens()


def test_history_protection_uses_one_index_seek_per_candidate(runtime):
    cold(runtime, "candidate")
    with runtime.store.transaction():
        for number in range(1000):
            runtime.store.save_intent(
                str(number),
                {"token_id": "history", "event_id": "event", "side": "BUY", "terminal": True},
            )
    queries = []
    runtime.store.db.set_trace_callback(queries.append)
    try:
        result = prune(runtime)
    finally:
        runtime.store.db.set_trace_callback(None)
    assert result["removed_ids"] == ("candidate",)
    lookup = [query for query in queries if "FROM intents" in query]
    assert len(lookup) == 1 and "INDEXED BY intents_token" in lookup[0]
    assert "WHERE token_id='candidate' LIMIT 1" in lookup[0]
    plan = runtime.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM intents INDEXED BY intents_token WHERE token_id=? LIMIT 1",
        ("candidate",),
    ).fetchall()
    assert any("SEARCH intents USING COVERING INDEX intents_token" in row[3] for row in plan)


def test_readonly_ledger_cannot_be_mutated(runtime):
    path = runtime.store.path
    cold(runtime, "candidate")
    before = financial_fingerprint(runtime.store.db)
    runtime.store.db.close()
    runtime.store.db = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None
    )

    result = prune(runtime)

    assert result["removed"] == 0 and result["skipped"]
    assert len(runtime.store.tokens()) == 1
    assert financial_fingerprint(runtime.store.db) == before


def test_write_failure_rolls_back_previous_metadata_deletions(runtime):
    cold(runtime, "first")
    cold(runtime, "second")
    runtime.store.db.execute("""CREATE TRIGGER controlled_full BEFORE DELETE ON tokens
        WHEN OLD.token_id='second' BEGIN SELECT RAISE(ABORT,'disk full'); END""")
    before = financial_fingerprint(runtime.store.db)

    result = prune(runtime)

    assert result["removed"] == 0 and result["skipped"]
    assert set(runtime.store.tokens()) == {"first", "second"}
    assert not runtime.store.db.in_transaction
    assert financial_fingerprint(runtime.store.db) == before


def test_external_writer_never_causes_default_five_second_wait(runtime):
    cold(runtime, "candidate")
    external = sqlite3.connect(runtime.store.path, isolation_level=None)
    external.execute("BEGIN IMMEDIATE")
    timeout = runtime.store.db.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        started = monotonic()
        result = prune(runtime)
        assert monotonic() - started < 0.5
        assert result["skipped"] and result["removed"] == 0
        assert runtime.store.db.execute("PRAGMA busy_timeout").fetchone()[0] == timeout
    finally:
        external.execute("ROLLBACK")
        external.close()


def test_exhausted_work_budget_is_observable_without_deletion(runtime, monkeypatch):
    cold(runtime, "candidate")
    ticks = iter([0, 1])
    monkeypatch.setattr("pm_nautilus.metadata_retention.monotonic", lambda: next(ticks, 1))
    result = prune(runtime)
    assert result["skipped"] == "time_budget" and result["removed"] == 0
    assert "candidate" in runtime.store.tokens()


def test_existing_transaction_and_faulted_runtime_do_no_work(runtime):
    cold(runtime, "candidate")
    with runtime.store.transaction():
        assert prune(runtime)["skipped"] == "transaction_in_progress"
    runtime.faulted = "uncommitted financial projection"
    assert prune(runtime)["skipped"] == "runtime_faulted"
    assert "candidate" in runtime.store.tokens()


@pytest.mark.parametrize(
    "changes", [{"limit": 129}, {"limit": 0}, {"time_budget": 10}, {"time_budget": float("nan")}]
)
def test_unbounded_requests_are_rejected(runtime, changes):
    arguments = {"time_budget": 0.1} | changes
    with pytest.raises(ValueError, match="bounded metadata retention"):
        prune_cold_metadata(runtime, 10 * DAY, **arguments)


def test_real_runtime_can_restart_and_validate_after_public_cache_eviction(tmp_path):
    from pm_nautilus.strategy import Runtime
    from test_runtime import setup

    owner, clock, original = setup(tmp_path)
    try:
        owner.store.db.execute("CREATE INDEX IF NOT EXISTS intents_token ON intents(token_id)")
        old_public = replace(
            original, token_id="cold-public", event_id="old", ends_ns=DAY, open=False
        )
        owner.add_tokens([old_public])
        before = financial_fingerprint(owner.store.db)

        result = prune_cold_metadata(owner, 10 * DAY, time_budget=0.1)
        for token_id in result["removed_ids"]:
            owner.tokens.pop(token_id, None)

        assert result["removed_ids"] == ("cold-public",)
        assert owner.validate()["ok"]
        assert financial_fingerprint(owner.store.db) == before
    finally:
        owner.close()
    reopened = Runtime(tmp_path / "test.sqlite", clock=clock)
    try:
        assert "cold-public" not in reopened.store.tokens()
        assert reopened.validate()["ok"]
    finally:
        reopened.close()
