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
