import asyncio
import pytest
from test_runtime import setup
from pm_nautilus.redemption import RedemptionService
from pm_nautilus.views import dashboard
from pm_nautilus.strategy import Runtime


@pytest.mark.parametrize("payout", [0, 500_000, 1_000_000])
def test_settlement_has_separate_receivable_and_cash(tmp_path, payout):
    r, clock, t = setup(tmp_path)
    r.book("1", [(15000, 100_000_000)], [(20000, 50_000_000)])
    r.start()
    r.pause()
    s = RedemptionService(r, None)
    s.identify(t, {"1": payout, "2": 1_000_000 - payout})
    claim = r.business["claims"][t.condition_id]
    assert r.cash() == 99_000_000
    assert not dashboard(r)["positions"]
    assert r.validate()["ok"]
    asyncio.run(s.advance(claim))
    assert claim["state"] == "CONFIRMED" and r.cash() == 99_000_000
    r.close()
    r = Runtime(tmp_path / "test.sqlite", clock=clock)
    claim = r.business["claims"][t.condition_id]
    s = RedemptionService(r, None)
    asyncio.run(s.advance(claim))
    assert claim["state"] == "CREDITED"
    assert r.cash() == 99_000_000 + 50 * payout
    assert r.validate()["ok"]
    assert not r.business["cycles"]
    asyncio.run(s.advance(claim))
    assert r.cash() == 99_000_000 + 50 * payout
    r.close()
