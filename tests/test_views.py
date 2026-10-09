from dataclasses import replace

from pm_nautilus.strategy import Runtime
from pm_nautilus.views import dashboard, portfolio_view, records
from test_runtime import setup


def test_position_valuation_uses_each_executable_bid_level(tmp_path):
    r, _, _ = setup(tmp_path)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    r.book("1", [(15_000, 1_000_000), (10_000, 2_000_000)], [(20_000, 50_000_000)])
    positions, portfolio = portfolio_view(r)
    assert positions[0]["quantity"] == "50"
    assert positions[0]["executableSellQuantity"] == "3"
    assert positions[0]["executableSellValue"] == "0.035"
    assert portfolio["positionValue"] == "0.035"
    assert portfolio["totalFunds"] == "99.035"
    assert portfolio["unrealizedPnl"] == "-0.965"
    r.close()


def test_failed_redemption_is_not_counted_as_receivable_or_pnl(tmp_path):
    r, _, t = setup(tmp_path)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    r.business["settled"].append(t.condition_id)
    r.business["claims"][t.condition_id] = {
        "state": "FAILED",
        "amount": 1_000_000,
        "cost": 1_000_000,
    }
    _, portfolio = portfolio_view(r)
    assert portfolio["pendingRedemption"] == "0"
    assert portfolio["failedRedemption"] == "1"
    assert portfolio["totalFunds"] is None
    assert portfolio["unrealizedPnl"] is None
    r.close()


def test_dashboard_projects_visible_page_and_retains_complete_diagnostics(tmp_path, monkeypatch):
    import pm_nautilus.views as views
    from pm_nautilus.books import Book

    r, _, t = setup(tmp_path)
    tokens = [
        replace(
            t, token_id=str(n), event_id=f"event-{n:03d}", ends_ns=t.ends_ns + n * 60_000_000_000
        )
        for n in range(2, 102)
    ]
    r.add_tokens(tokens)
    for token in r.tokens.values():
        book = Book()
        book.snapshot([(15_000, 100_000_000)], [(20_000, 50_000_000)], r.now)
        r.books[token.token_id] = book
    r.books["101"].ready = False
    r.evaluations = {eid: ("NO_WINNER", None) for eid in r.events}
    r.evaluations["event-101"] = ("INCOMPLETE", None)
    intent = {"event_id": "event-100", "token_id": "100", "side": "SELL", "terminal": False}
    r.store.save_intent("pending", intent)
    original = views.token_view
    calls = []

    def project(runtime, token):
        calls.append(token.token_id)
        return original(runtime, token)

    monkeypatch.setattr(views, "token_view", project)
    sql = []
    r.store.db.set_trace_callback(sql.append)
    view = dashboard(r, limit=2)
    r.store.db.set_trace_callback(None)
    scan = view["marketScan"]
    assert len(calls) == 2
    assert [event["eventId"] for event in scan["events"]] == ["event-101", "event-100"]
    assert scan["events"][0]["outcomes"][0]["currentSellPriceStatus"] == "NOT_READY"
    assert scan["events"][1]["locked"] is True
    assert scan["displayEventCount"] == 101
    assert scan["pendingEventCount"] == 1 and scan["eventCount"] == 0
    assert scan["diagnostics"]["pendingEvents"] == [
        {
            "eventId": "event-101",
            "pendingReason": "AWAITING_FULL_BOOKS",
            "missingBookTokenIds": ["101"],
        }
    ]
    # One reservation query and one batch of event locks, independent of event count.
    assert sum("SELECT order_id,payload FROM intents" in statement for statement in sql) == 2
    r.close()


def test_record_projection_new_fills_metadata_limit_and_read_only_refresh(tmp_path):
    r, clock, t = setup(tmp_path)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    first = records(r, 20)
    assert first["totalCount"] == 1 and first["records"][0]["quantity"] == "50"
    before = r.store.db.total_changes
    sql = []
    r.store.db.set_trace_callback(sql.append)
    assert records(r, 1) == first
    r.store.db.set_trace_callback(None)
    assert r.store.db.total_changes == before
    assert not any("FROM intents" in statement for statement in sql)

    r.book("1", [(40_000, 5_000_000), (35_000, 5_000_000)], [(20_000, 50_000_000)])
    partial = records(r, 1)
    assert partial["totalCount"] == 2
    row = partial["records"][0]
    assert row["type"] == "PARTIAL_CLOSE" and row["quantity"] == "10"
    assert row["price"] == "0.0375" and row["realizedPnl"] == "0.175"
    r.book("1", [(35_000, 45_000_000)], [(20_000, 50_000_000)])
    closed = records(r, 20)
    assert closed["totalCount"] == 3
    assert closed["records"][0]["type"] == "CLOSE"
    assert closed["records"][0]["quantity"] == "40"
    r.add_tokens([replace(t, event_title="Current title", slug="current-url")])
    refreshed = records(r, 20)
    assert all(row["eventTitle"] == "Current title" for row in refreshed["records"])
    assert all(row["marketUrl"].endswith("/current-url") for row in refreshed["records"])
    refreshed["records"][0]["amount"] = "changed by caller"
    assert records(r, 20)["records"][0]["amount"] != "changed by caller"
    assert records(r, 0) == {"records": [], "totalCount": 3}
    expected = records(r, 20)
    r.close()
    r = Runtime(tmp_path / "test.sqlite", clock=clock)
    assert records(r, 20) == expected
    r.store.reset()
    assert records(r, 20) == {"records": [], "totalCount": 0}
    r.close()


def test_record_projection_recovers_missing_intent(tmp_path):
    r, _, _ = setup(tmp_path)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    order_id, intent = next(iter(r.store.intents().items()))
    r.store.db.execute("DELETE FROM intents WHERE order_id=?", (order_id,))
    assert records(r, 20) == {"records": [], "totalCount": 0}
    r.store.save_intent(order_id, intent)
    assert records(r, 20)["totalCount"] == 1
    r.close()


def test_record_projection_aggregates_a_later_fill_for_the_same_order(tmp_path):
    r, _, _ = setup(tmp_path)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 20_000_000), (30_000, 20_000_000)])
    r.start()
    r.pause()
    fill = r.store.db.execute(
        "SELECT seq,event_id,kind,payload FROM native_events "
        "WHERE kind='OrderFilled' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    r.store.db.execute("DELETE FROM native_events WHERE seq=?", (fill[0],))
    assert records(r, 20)["records"][0]["quantity"] == "20"
    r.store.db.execute(
        "INSERT INTO native_events(event_id,kind,payload) VALUES(?,?,?)", tuple(fill)[1:]
    )
    view = records(r, 20)
    assert view["totalCount"] == 1
    assert view["records"][0]["type"] == "OPEN"
    assert view["records"][0]["quantity"] == "40"
    assert view["records"][0]["amount"] == "1"
    assert view["records"][0]["price"] == "0.025"
    r.close()


def test_record_projection_skips_lifecycle_payloads_and_consumes_them_once(tmp_path, monkeypatch):
    from types import SimpleNamespace

    r, _, _ = setup(tmp_path)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    expected = records(r, 20)
    lifecycle = r.store.db.execute(
        "SELECT kind,payload FROM native_events WHERE kind!='OrderFilled' LIMIT 1"
    ).fetchone()
    r.store.db.execute(
        "INSERT INTO native_events(event_id,kind,payload) VALUES(?,?,?)",
        ("another-lifecycle", lifecycle[0], lifecycle[1]),
    )

    def unexpected(payload):
        raise AssertionError("Historical fills and lifecycle payloads must not be deserialized")

    with monkeypatch.context() as patch:
        patch.setattr(r.store, "serializer", SimpleNamespace(deserialize=unexpected))
        assert records(r, 20) == expected
        head = r._record_projection.cursor
        assert head == r.store.db.execute("SELECT max(seq) FROM native_events").fetchone()[0]
        assert records(r, 20) == expected
    r.close()


def test_record_window_retains_count_order_and_basis_after_eviction(tmp_path, monkeypatch):
    import json
    import pm_nautilus.views as views

    monkeypatch.setattr(views, "RECORD_CACHE_ORDERS", 2)
    r, _, _ = setup(tmp_path)
    r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
    r.start()
    r.pause()
    r.book("1", [(35_000, 10_000_000)], [(20_000, 50_000_000)])
    r.book("1", [(35_000, 50_000_000)], [(20_000, 50_000_000)])
    initial = records(r, 2)
    assert initial["totalCount"] == 3
    assert len(r._record_projection.groups) == 2
    assert r._record_projection.qty == r._record_projection.basis == {}
    fills = [
        json.loads(row[0])
        for row in r.store.db.execute(
            "SELECT payload FROM native_events WHERE kind='OrderFilled' ORDER BY seq"
        )
    ]

    def append_fill(source, order_id, label, quantity, amount):
        payload = json.loads(json.dumps(source))
        payload.update(client_order_id=order_id, trade_id=label, last_qty=f"{quantity}.000000")
        payload["info"].update(pm_amount=amount, pm_gross=quantity * 1_000_000)
        r.store.db.execute(
            "INSERT INTO native_events(event_id,kind,payload) VALUES(?,?,?)",
            (label, "OrderFilled", json.dumps(payload).encode()),
        )

    # A late BUY for the evicted original order opens inventory again without
    # moving that old order to the newest page or incrementing totalCount.
    append_fill(fills[0], fills[0]["client_order_id"], "late-buy", 10, 200_000)
    assert records(r, 2) == initial
    assert r._record_projection.qty == {"1": 10_000_000}
    assert r._record_projection.basis == {"1": 200_000}
    intent = r.store.intent(fills[-1]["client_order_id"])
    r.store.save_intent("late-close", intent)
    append_fill(fills[-1], "late-close", "late-close-fill", 10, 350_000)
    current = records(r, 2)
    assert current["totalCount"] == 4 and len(r._record_projection.groups) == 2
    assert current["records"][0]["id"] == "late-close"
    assert current["records"][0]["type"] == "CLOSE"
    assert current["records"][0]["realizedPnl"] == "0.15"
    assert r._record_projection.qty == r._record_projection.basis == {}
    # A larger in-process caller can rebuild the required window; HTTP remains
    # limited to 10000. Older first-fill order stays unchanged across rebuild.
    expanded = records(r, 3)
    assert expanded["totalCount"] == 4
    assert expanded["records"][:2] == current["records"]
    assert expanded["records"][2]["id"] == initial["records"][1]["id"]
    r.store.reset()
    assert records(r, 2) == {"records": [], "totalCount": 0}
    r.close()
