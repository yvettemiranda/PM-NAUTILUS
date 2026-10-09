"""Local health recovery and history-scaled views; no private clients or signing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pm_nautilus.books import Book
from pm_nautilus.config import Preferences
from pm_nautilus.live import LiveExecution, _LedgerGate
from pm_nautilus.rules import Fees, Token
from pm_nautilus.store import Store
from pm_nautilus.strategy import Runtime
from pm_nautilus.views import dashboard

DAY = 86_400_000_000_000


@pytest.mark.parametrize("valid", [True, False])
def test_live_health_recovers_only_after_complete_validation(monkeypatch, valid):
    values = {}
    owner = SimpleNamespace(
        status="RUNNING",
        store=SimpleNamespace(_closed=False, put=lambda key, value: values.update({key: value})),
        validate_async=AsyncMock(
            return_value={"ok": valid, "errors": [] if valid else ["mismatch"]}
        ),
        drain=Mock(),
    )

    def pause():
        owner.status = "PAUSED"

    owner.pause = Mock(side_effect=pause)
    client = SimpleNamespace(
        owner=owner,
        ready=True,
        open_orders_clear=True,
        sync_owned=AsyncMock(side_effect=[ConnectionError("temporary outage"), None]),
        generate_position_status_reports=AsyncMock(),
        _reconcile_requested=asyncio.Event(),
        _ledger_gate=_LedgerGate(),
        _ledger_validation=None,
    )
    client._validate_ledger = LiveExecution._validate_ledger.__get__(client)
    client._invalidate_ledger = LiveExecution._invalidate_ledger.__get__(client)

    async def check_open_orders():
        client.open_orders_clear = True
        client._reconcile_requested.set()

    client._check_open_orders = AsyncMock(side_effect=check_open_orders)
    rounds = []

    async def finish_round(_delay):
        rounds.append((client.ready, owner.status, values.get("live_error")))
        if len(rounds) == 1:
            # A transport failure must not claim that custody or the journal was checked.
            owner.validate_async.assert_not_awaited()
            client.generate_position_status_reports.assert_not_awaited()
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr("pm_nautilus.live.asyncio.sleep", finish_round)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(LiveExecution.maintain(client))

    assert rounds[0] == (False, "PAUSED", "ConnectionError")
    assert rounds[1] == (valid, "PAUSED", None if valid else "ValueError")
    assert client.sync_owned.await_count == 2
    client.generate_position_status_reports.assert_awaited_once_with(None)
    owner.validate_async.assert_awaited_once_with()
    assert owner.drain.call_count == int(valid)


def test_dashboard_filters_history_and_batches_active_event_locks(tmp_path, monkeypatch):
    """Closed history must not be decoded once per Event on each 500ms refresh."""
    store = Store(tmp_path / "history.sqlite")
    try:
        r = SimpleNamespace(
            store=store,
            mode="TEST",
            business=store.get("business"),
            preferences=Preferences(),
            tokens={},
            books={},
            events={},
            evaluations={},
            now=DAY,
            status="PAUSED",
            monitored=set(),
            cash=lambda: 100_000_000,
        )
        r.active_intents = lambda eid: Runtime.active_intents(r, eid)
        r.held_cash = lambda: Runtime.held_cash(r)
        for number in range(200):
            tid, eid = str(number), f"event-{number}"
            r.tokens[tid] = Token(
                tid,
                eid,
                f"market-{number}",
                f"condition-{number}",
                "YES",
                2,
                False,
                0,
                10 * DAY,
                Fees(False, 0),
                5_000_000,
                1_000,
            )
            r.books[tid] = Book()
            r.events[eid] = {tid}
            r.monitored.add(tid)

        def intent(eid, terminal):
            return {
                "event_id": eid,
                "token_id": "0",
                "side": "BUY",
                "cash": 1_000_000,
                "spent": 400_000,
                "terminal": terminal,
            }

        with store.transaction():
            for number in range(2_000):
                store.save_intent(f"old-{number}", intent(f"event-{number % 200}", True))
            store.save_intent("pending", intent("event-0", False))

        original = store.intents
        filtered_calls = []

        def active_only(*args, **kwargs):
            assert kwargs.get("active_only") is True, "Dashboard loaded unfiltered history"
            filtered_calls.append(kwargs.get("event_id"))
            result = original(*args, **kwargs)
            assert all(not i["terminal"] for i in result.values())
            return result

        monkeypatch.setattr(store, "intents", active_only)
        queries = []
        store.db.set_trace_callback(queries.append)
        result = dashboard(r, limit=200)
        store.db.set_trace_callback(None)

        # Reservation and lock projections each load the active set once.
        assert filtered_calls == [None, None]
        locked = [event["eventId"] for event in result["marketScan"]["events"] if event["locked"]]
        assert locked == ["event-0"]
        assert result["portfolio"]["reservedCash"] == "0.6"
        assert result["portfolio"]["availableCash"] == "99.4"
        event_queries = [
            query for query in queries if "FROM intents" in query and "event_id=" in query
        ]
        assert not event_queries
        batch_queries = [
            query for query in queries if "SELECT order_id,payload FROM intents" in query
        ]
        assert len(batch_queries) == 2
        for query in set(batch_queries):
            assert "WHERE" in query.upper()
            assert "json_extract(payload,'$.terminal')=0" in query
            plan = store.db.execute("EXPLAIN QUERY PLAN " + query).fetchall()
            assert any("USING INDEX intents_open" in row[3] for row in plan), plan
    finally:
        store.close()
