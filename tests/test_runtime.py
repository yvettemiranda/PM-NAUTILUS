from nautilus_trader.common.component import TestClock
from pm_nautilus.strategy import Runtime
from pm_nautilus.rules import Token, Fees

DAY = 86400000000000


def setup(tmp_path, fees=False):
    clock = TestClock()
    clock.set_time(DAY)
    r = Runtime(tmp_path / "test.sqlite", clock=clock)
    t = Token(
        "1",
        "event",
        "market",
        "condition",
        "YES",
        2,
        False,
        0,
        10 * DAY,
        Fees(fees, 40000 if fees else 0),
        5_000_000,
        1000,
    )
    r.add_tokens([t])
    return r, clock, t


def test_native_buy_target_sell_and_recovery(tmp_path):
    r, clock, t = setup(tmp_path)
    r.book("1", [(15000, 100_000_000)], [(20000, 50_000_000)])
    r.start()
    assert len(r.native.cache.orders()) == 1
    assert r.business["cycles"]["event"]["quantity"] == 50_000_000
    assert r.cash() == 99_000_000
    r.pause()
    r.book("1", [(35000, 5_000_000), (10000, 1_000_000)], [(20000, 50_000_000)])
    assert r.business["cycles"]["event"]["quantity"] == 45_000_000
    assert r.cash() == 99_175_000
    r.book("1", [(35000, 5_000_000), (10000, 2_000_000)], [(20000, 50_000_000)])
    assert r.business["cycles"]["event"]["quantity"] == 45_000_000
    assert r.validate()["ok"]
    r.close()
    r = Runtime(tmp_path / "test.sqlite", clock=clock)
    assert r.status == "PAUSED"
    assert r.cash() == 99_175_000
    assert r.validate()["ok"]
    r.close()


def test_native_fee_net_position(tmp_path):
    r, clock, t = setup(tmp_path, True)
    r.book("1", [(90000, 100_000_000)], [(100000, 10_000_000)])
    r.start()
    assert r.business["cycles"]["event"]["quantity"] == 9_640_000
    assert r.cash() == 99_000_000
    assert r.validate()["ok"]
    r.close()


def test_projection_write_failure_pauses_and_replays_once(tmp_path, monkeypatch):
    import sqlite3
    import pytest

    r, clock, t = setup(tmp_path)
    r.book("1", [(15000, 100_000_000)], [(20000, 50_000_000)])
    original = r.store.save_book

    def fail(*args):
        raise sqlite3.OperationalError("controlled disk failure")

    monkeypatch.setattr(r.store, "save_book", fail)
    with pytest.raises(sqlite3.OperationalError):
        r.start()
    assert r.status == "PAUSED" and not r.validate()["ok"]
    assert not r.business["cycles"]
    monkeypatch.setattr(r.store, "save_book", original)
    r.book("1", [(15000, 100_000_000)], [(20000, 50_000_000)])
    r.close()
    r = Runtime(tmp_path / "test.sqlite", clock=clock)
    assert r.business["cycles"]["event"]["quantity"] == 50_000_000
    assert r.test_cash() == 99_000_000
    assert r.validate()["ok"]
    r.close()


def test_cash_buy_uses_budget_not_worst_price_notional(tmp_path):
    r, clock, t = setup(tmp_path)
    r.update_preferences({}, capital="1")
    r.book("1", [(15000, 100_000_000)], [(20000, 20_000_000), (30000, 100_000_000)])
    r.start()
    c = r.business["cycles"]["event"]
    assert c["spent"] == 1_000_000 and c["quantity"] == 40_000_000
    assert sorted(x["price"] for x in r.business["targets"].values()) == [30000, 45000]
    assert r.cash() == 0 and r.validate()["ok"]
    r.close()


def test_stop_partial_exit_and_persistent_ban(tmp_path):
    r, clock, t = setup(tmp_path)
    r.book("1", [(90000, 100_000_000)], [(100000, 10_000_000)])
    r.start()
    r.pause()
    clock.set_time(DAY + 1_000_000_000)
    r.book("1", [(30000, 4_000_000)], [(100000, 10_000_000)])
    assert r.business["cycles"]["event"]["stop"]["state"] == "ARMED"
    clock.set_time(DAY + 31_000_000_000)
    r.book("1", [(30000, 5_000_000)], [(100000, 10_000_000)])
    assert "event" in r.business["banned"]
    assert r.business["cycles"]["event"]["quantity"] == 5_000_000
    assert not r.business["targets"]
    r.close()
    r = Runtime(tmp_path / "test.sqlite", clock=clock)
    assert r.business["cycles"]["event"]["stop"]["state"] == "EXITING"
    r.book("1", [(200000, 10_000_000)], [(100000, 10_000_000)])
    assert not r.business["cycles"]
    r.start()
    assert not r.business["cycles"]
    r.close()


def test_fill_replay_preserves_rules_at_receipt(tmp_path, monkeypatch):
    import sqlite3
    import pytest
    from pm_nautilus.config import Preferences
    from nautilus_trader.model.events import OrderFilled

    r, clock, t = setup(tmp_path)
    r.book("1", [(15000, 100_000_000)], [(20000, 50_000_000)])

    original = r._project_transaction

    def fail(seq, event, intent, token):
        if isinstance(event, OrderFilled):
            raise sqlite3.OperationalError("interrupted projection")
        return original(seq, event, intent, token)

    monkeypatch.setattr(r, "_project_transaction", fail)
    with pytest.raises(sqlite3.OperationalError):
        r.start()
    # Persisted Fill is ahead of the projection. A later saved configuration
    # must not change that fact's target or first-fill freeze on recovery.
    r.store.put(
        "preferences",
        Preferences(
            orderAmount="2",
            stopLossEnabled=False,
            stopLossMultiplier="0.8",
            targetSellPriceMultiplier="4",
        ).model_dump(mode="json"),
    )
    r.native.stop()
    r.store.close()
    r = Runtime(tmp_path / "test.sqlite", clock=clock)
    cycle = r.business["cycles"]["event"]
    assert cycle["budget"] == 1_000_000
    assert cycle["stop"]["enabled"] is True and cycle["stop"]["multiplier"] == 400000
    assert [v["price"] for v in r.business["targets"].values()] == [30000]
    assert r.validate()["ok"]
    r.close()


def test_cross_event_current_ready_then_lifecycle(tmp_path):
    from dataclasses import replace
    from copy import deepcopy

    r, clock, t = setup(tmp_path)
    a = replace(t, token_id="2", event_id="earlier", market_id="m2", opened_ns=0, ends_ns=20 * DAY)
    r.add_tokens([a])
    r.update_preferences({}, capital="1")
    for tid in ("1", "2"):
        r.book(tid, [(15000, 100_000_000)], [(20000, 50_000_000)])
    r.evaluations["earlier"] = "NO_WINNER", None
    before = deepcopy(r.business)
    r.evaluate("earlier", dispatch=False)
    assert r.business == before and not r.store.has_history()
    # Recreate an out-of-date previous READY grouping, as on a batched update.
    r.evaluations["earlier"] = "NO_WINNER", None
    r.start()
    # Both are currently READY; earlier has 5% progress versus event's 10%.
    assert list(r.business["cycles"]) == ["earlier"]
    r.close()


def test_multilevel_fak_records_group_native_fills(tmp_path):
    from pm_nautilus.views import records

    r, clock, t = setup(tmp_path)
    r.book("1", [(15000, 100_000_000)], [(20000, 20_000_000), (30000, 20_000_000)])
    r.start()
    page = records(r, 20)
    assert page["totalCount"] == 1
    row = page["records"][0]
    assert row["type"] == "OPEN" and row["quantity"] == "40"
    assert row["amount"] == "1" and row["price"] == "0.025"
    assert len(r.business["targets"]) == 2  # UI grouping never merges actual targets.
    r.close()


def test_existing_cycle_freeze_and_new_fill_target_use_current_settings(tmp_path):
    r, clock, t = setup(tmp_path)
    r.book("1", [(90000, 100_000_000)], [(100000, 5_000_000)])
    r.start()
    assert r.business["cycles"]["event"]["spent"] == 500000
    r.update_preferences(
        {"orderAmount": "2", "targetSellPriceMultiplier": "3", "stopLossEnabled": False}
    )
    r.book("1", [(90000, 100_000_000)], [(100000, 10_000_000)])
    cycle = r.business["cycles"]["event"]
    assert cycle["spent"] == cycle["budget"] == 1_000_000
    assert cycle["stop"]["enabled"] is True
    assert sorted(x["price"] for x in r.business["targets"].values()) == [150000, 300000]
    assert r.validate()["ok"]
    r.close()
