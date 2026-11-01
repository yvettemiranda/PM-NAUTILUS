"""The CLOB's $1 marketable-BUY floor applies identically to TEST and LIVE decisions."""

from pm_nautilus.config import Preferences
from pm_nautilus.order_terms import MIN_MARKETABLE_BUY_NOTIONAL
from pm_nautilus.rules import Fees, meets_marketable_buy_minimum, preview, signed_buy_notional

from test_rules import token
from test_runtime import setup


def test_floor_uses_the_same_fee_safe_signed_amount_as_the_signer():
    assert MIN_MARKETABLE_BUY_NOTIONAL == 1_000_000
    asks, bids = [(20_000, 100_000_000)], [(15_000, 100_000_000)]
    free = token(min_size=1_000_000)
    paid = token(min_size=1_000_000, fees=Fees(True, 40_000, 1))

    exact = preview(free, Preferences(), bids, asks, 1_000_000, 1_000_000)
    assert meets_marketable_buy_minimum(exact, 1_000_000)  # exactly $1 is accepted

    # 1U with a 4% fee signs only 0.96U, which the venue rejects.
    short = preview(paid, Preferences(), bids, asks, 1_000_000, 1_000_000)
    assert short is not None
    assert signed_buy_notional(1_000_000, paid.fees, paid.tick, 20_000) == 960_000
    assert not meets_marketable_buy_minimum(short, 1_000_000)

    enough = preview(paid, Preferences(), bids, asks, 1_050_000, 1_050_000)
    assert signed_buy_notional(1_050_000, paid.fees, paid.tick, 20_000) == 1_000_000
    assert meets_marketable_buy_minimum(enough, 1_050_000)


def test_test_runtime_never_buys_under_the_floor_and_buys_once_it_is_cleared(tmp_path):
    r, _, t = setup(tmp_path, fees=True)  # default 1U budget, 4% fee
    try:
        r.book("1", [(15_000, 100_000_000)], [(20_000, 50_000_000)])
        r.start()
        assert not r.store.intents() and not r.business["cycles"]
        assert r.cash() == 100_000_000
        r.pause()
        r.update_preferences({"orderAmount": "1.05"})
        r.start()
        cycle = r.business["cycles"][t.event_id]
        assert cycle["quantity"] == 50_000_000 and cycle["spent"] <= 1_050_000
        assert r.validate()["ok"]
    finally:
        r.close()


def test_remaining_cycle_budget_under_the_floor_does_not_add(tmp_path):
    r, _, t = setup(tmp_path)  # fee-free, 1U budget
    try:
        r.update_preferences({"orderAmount": "1.5"})
        # Only 0.6U of depth: the remaining 0.9U is an unsignable add-on.
        r.book("1", [(15_000, 100_000_000)], [(20_000, 30_000_000)])
        r.start()
        cycle = r.business["cycles"][t.event_id]
        assert (cycle["quantity"], cycle["spent"]) == (30_000_000, 600_000)
        orders = len(r.native.cache.orders())
        r.book("1", [(15_000, 100_000_000)], [(20_000, 80_000_000)])
        assert len(r.native.cache.orders()) == orders
        assert r.business["cycles"][t.event_id]["spent"] == 600_000
    finally:
        r.close()
