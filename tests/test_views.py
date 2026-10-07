from pm_nautilus.views import portfolio_view
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
